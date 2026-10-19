/**
 * SecurityPanel (Settings → Security): the governance policy viewer and its central
 * distribution block, the posture disclosure, and the third-party-apps trust card.
 *
 * Three suites split from this file cover more of the panel: the denied-commands rules
 * and their search in SecurityPanel.deniedCommands.test.tsx, registry trust in
 * SecurityPanel.registryTrust.test.tsx, and the inspector rail and the redaction pane in
 * SecurityPanel.rail.test.tsx. Each keeps its own copy of the api mock below; the
 * fixtures they share with this file are in src/test/SecurityPanelTestUtils.tsx.
 *
 * The trusted-apps and allow-all describes set only their own mocks and read the rail's
 * `kirocrewConfig` and `tailnetStatus` answers from the posture describe's `beforeEach`, so
 * they stay behind it.
 */
import { ApiError } from '../../api/client'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * SecurityPanel drives all its mutations through the `api` client methods.  We
 * mock those methods so no network I/O happens; each returns the (mutated)
 * snapshot the panel then re-renders from.  `securityPosture` feeds the Live
 * Security Posture card, and `governancePolicy` feeds the ceiling viewer.
 */
vi.mock('../../api/client', () => ({
  // The real class, not a stub: `trustFailureMessage` branches on `instanceof
  // ApiError` to decide whether a structured body is available to read.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    deniedCommands: vi.fn(),
    toggleBuiltinDeniedCommand: vi.fn(),
    setDeniedCommandsDisableAll: vi.fn(),
    addUserDeniedCommand: vi.fn(),
    toggleUserDeniedCommand: vi.fn(),
    deleteUserDeniedCommand: vi.fn(),
    redactionAllowedHosts: vi.fn(),
    redactionRevokeHost: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    // Read + write for the third-party-app execution toggle. Also consumed by
    // YoloDurationCard, which tolerates an unresolved read.
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    // Read by the rail on every mount to summarise the tailnet-origin section.
    // Present here so the rail's own read is a resolved query rather than a
    // crash on an undefined queryFn; the section's behaviour is covered in
    // SecurityPanel.tailnet.test.tsx.
    tailnetStatus: vi.fn(),
    // Read by the rail on every mount to summarise the flagged-file-delivery
    // section, for the same reason as `tailnetStatus` above. The section's own
    // behaviour is covered in SecurityPanel.fileDelivery.test.tsx.
    fileDeliveryConsent: vi.fn(),
    revokeFileDeliveryConsent: vi.fn(),
    listTrustedApps: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    setTrustAllApps: vi.fn(),
    // Read + the two writes behind the registry git-identity trust section.
    listTrustedRegistries: vi.fn(),
    grantTrustedRegistry: vi.fn(),
    revokeTrustedRegistry: vi.fn(),
  },
}))

import { api } from '../../api/client'
import type { GovernanceDistributionData, GovernancePolicyData, TrustedAppsData } from '../../api/client'
import { SecurityPanel, trustFailureMessage, humaniseScopeLeaf } from './SecurityPanel'
import { i18nT } from '../../i18n/t'
import { TAILNET_OFF, CONSENT_NONE, snapshot, posture, govNoPolicy } from '../../test/SecurityPanelTestUtils'

/** Copy is asserted through `i18nT`, not literal English.
 *
 * The trusted-apps catalog entries are authored separately from this component,
 * so a literal-English assertion here would be red until they land and would have
 * to be edited again when they do. Resolving the same key the component resolves
 * asserts the BEHAVIOUR (this control is wired to that key) and stays green
 * across the catalog landing — while still failing loudly if the component starts
 * rendering a different key or hardcodes English. */
const K = 'pages.settings.securityPanel.trustedApps'
const T = {
  allowAllLabel: () => i18nT(`${K}.allow_all_label`),
  empty: () => i18nT(`${K}.empty`),
  revoke: () => i18nT(`${K}.revoke`),
  trustedBadge: () => i18nT(`${K}.trusted_badge`),
  revokeDisables: (name: string) => i18nT(`${K}.revoke_disables`, { name }),
  ineffectiveLabel: () => i18nT(`${K}.ineffective_label`),
  ineffectiveDescription: () => i18nT(`${K}.ineffective_description`),
  allowAllAck: () => i18nT(`${K}.allow_all_confirm_ack`),
  confirmBtn: () => i18nT('components.trustDropdown.trust'),
  revokeConfirmBody: (name: string) => i18nT(`${K}.revoke_confirm_body`, { name }),
  revokeConfirmOk: () => i18nT(`${K}.revoke_confirm_ok`),
  cancel: () => i18nT('pages.settings.securityPanel.cancel'),
}

/** A governed governance snapshot exercising every archetype + a profile. */
function govGoverned(overrides: Partial<GovernancePolicyData> = {}): GovernancePolicyData {
  return {
    version: 1,
    has_policy: true,
    profile: 'host-tight',
    unavailable: false,
    scopes: [
      { scope: 'tools', archetype: 'ruleset', governed: true, source: 'policy+profile', detail: { mode: 'intersect', components: [{ mode: 'allow', allow_count: 3, deny_count: 0 }, { mode: 'allow', allow_count: 1, deny_count: 0 }] } },
      { scope: 'commands', archetype: 'ruleset', governed: true, source: 'policy', detail: { mode: 'deny', allow_count: 0, deny_count: 2 } },
      { scope: 'mcp', archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} },
      { scope: 'channels', archetype: 'scopedmap', governed: true, source: 'policy', detail: { members: { mode: 'allow', allow_count: 1, deny_count: 0 }, posture: { slack: { allowed_enterprise_ids: { mode: 'allow', allow_count: 1, deny_count: 0 } } } } },
      { scope: 'sandbox.min_level', archetype: 'ordinal', governed: true, source: 'policy', detail: { scale: 'sandbox', floor: 'cc' } },
      { scope: 'capabilities.cron', archetype: 'capability', governed: true, source: 'policy', detail: { enabled: false, inner: {} } },
      { scope: 'capabilities.spawn', archetype: 'capability', governed: true, source: 'policy', detail: { enabled: true, inner: { agents: { mode: 'allow', allow_count: 1, deny_count: 0 } } } },
      { scope: 'capabilities.messaging', archetype: 'capability', governed: false, source: 'ungoverned', detail: {} },
    ],
    ...overrides,
  }
}

/** A configured central-distribution posture.
 *
 * The defaults describe the state a governed fleet actually runs in — an https
 * source, a live poller on a 15-minute interval, a warm cache, and a poll that found
 * nothing to change — so each case below overrides only the field it is about. */
function govDistribution(overrides: Partial<GovernanceDistributionData> = {}): GovernanceDistributionData {
  return {
    configured: true,
    source_scheme: 'https',
    refresh_interval_seconds: 900,
    max_cache_age_seconds: 86400,
    on_unavailable: 'fail_closed',
    refresher_running: true,
    cache_present: true,
    cache_age_seconds: 120,
    last_refresh_status: 'unchanged',
    last_refresh_age_seconds: 30,
    error_code: '',
    ...overrides,
  }
}

/** A trusted-apps snapshot: two per-app grants, blanket flag off. */
function trusted(overrides: Partial<TrustedAppsData> = {}): TrustedAppsData {
  return {
    apps: ['launchdarkly', 'oncall-radar'],
    ineffective: [],
    allowAll: false,
    ...overrides,
  }
}

/** Stored names the gate IGNORES: a capital and a traversal-ish token, both
 *  outside the app-name charset, so neither can ever admit anything. */
const INEFFECTIVE = ['LD-App', '..']

describe('humaniseScopeLeaf', () => {
  it('title-cases each word of a snake_case or kebab-case leaf', () => {
    expect(humaniseScopeLeaf('external_access')).toBe('External Access')
    expect(humaniseScopeLeaf('capability_install')).toBe('Capability Install')
    expect(humaniseScopeLeaf('theme-persona')).toBe('Theme Persona')
    expect(humaniseScopeLeaf('cron')).toBe('Cron')
  })

  it('does not emit stray or doubled spaces from odd separators', () => {
    // A leading/trailing/doubled separator is an operator-authored identifier we
    // do not control, and the panel renders the result verbatim.
    expect(humaniseScopeLeaf('_leading')).toBe('Leading')
    expect(humaniseScopeLeaf('trailing_')).toBe('Trailing')
    expect(humaniseScopeLeaf('double__sep')).toBe('Double Sep')
    expect(humaniseScopeLeaf('')).toBe('')
  })
})

describe('SecurityPanel — governance policy viewer', () => {  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
    ;(api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mockResolvedValue(CONSENT_NONE)
  })

  it('shows the standalone "no enterprise policy" state when has_policy is false', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('No enterprise policy in effect')).toBeInTheDocument()
    expect(
      screen.getByText(/No policy or host profile restricts the host surface \(standalone mode\)/),
    ).toBeInTheDocument()
    // No governed rows are rendered in standalone mode.
    expect(screen.queryByText('policy ∩ profile')).not.toBeInTheDocument()
  })

  it('humanises the label of a companion-registered scope the core has no key for', async () => {
    // A scope registered through `register_scope` by an edition reaches this panel
    // (the snapshot endpoint iterates SCOPE_CATALOG) but can never have an i18n
    // key, because the core does not know it exists. Governed-scope leaves are
    // snake_case, so the raw fallback rendered `External_access` mid-panel.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        scopes: [
          {
            scope: 'capabilities.external_access',
            archetype: 'capability',
            governed: true,
            source: 'policy',
            detail: { enabled: true, inner: { registry_hosts: { mode: 'allow', allow_count: 2, deny_count: 0 } } },
          },
        ],
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('External Access')).toBeInTheDocument()
    expect(screen.queryByText('External_access')).not.toBeInTheDocument()
  })

  it('names a pinned social-share ceiling after the menu entry it withdraws', async () => {
    // A user whose "Share as image" entry vanished reads THIS row to learn why;
    // the humanised leaf ("Social Share") would not connect to the feature they
    // lost, and would render in English in every locale.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        scopes: [
          {
            scope: 'capabilities.social_share',
            archetype: 'capability',
            governed: true,
            source: 'policy',
            detail: { enabled: false, inner: {} },
          },
        ],
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Share as image')).toBeInTheDocument()
    expect(screen.queryByText('Social Share')).not.toBeInTheDocument()
  })

  it('renders governed + ungoverned rows with effective state and source', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govGoverned())
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    // Policy + profile badges.
    expect(await screen.findByText('Policy v1')).toBeInTheDocument()
    expect(screen.getByText('Profile: host-tight')).toBeInTheDocument()

    // POSTURE labels only — counts, never rule contents (the ceiling the agent
    // is fenced from). Ruleset (deny) → "Block-list · N rules"; capability off →
    // "Disabled by policy"; ordinal → "Floor: cc"; capability on → inner count.
    expect(screen.getByText(/Block-list · 2 rules/)).toBeInTheDocument()
    expect(screen.getByText('Disabled by policy')).toBeInTheDocument()
    expect(screen.getByText('Floor: cc')).toBeInTheDocument()
    expect(screen.getByText(/Enabled · agents: Allow-list · 1 rule/)).toBeInTheDocument()
    // The raw deny pattern must never appear in the DOM.
    expect(screen.queryByText(/git push\*/)).not.toBeInTheDocument()

    // policy+profile intersection badge is shown for the composed tools scope.
    expect(screen.getAllByText('policy ∩ profile').length).toBeGreaterThan(0)
    // A source badge is shown for EVERY governed source, not only the composed
    // case — a policy-only scope (e.g. commands) shows a "policy" badge.
    expect(screen.getAllByText('policy').length).toBeGreaterThan(0)

    // An ungoverned scope (messaging) shows the muted "Not restricted".
    expect(screen.getAllByText('Not restricted').length).toBeGreaterThan(0)
  })

  it('shows a soft notice when governance resolution is unavailable', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govNoPolicy({ unavailable: true }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText(/Governance status is temporarily unavailable/)).toBeInTheDocument()
  })

  // The host profile pins OFF capabilities the host process never performs (cron,
  // messaging, spawn), while the surfaces that do perform them enable those under
  // their own profiles. Labelling such a row "Disabled by policy" reported a
  // working feature as switched off, so the row must name whose ceiling it is.
  it('labels a host-profile capability pin as surface-scoped, not install-wide', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        scopes: [
          {
            scope: 'capabilities.cron',
            archetype: 'capability',
            governed: true,
            source: 'profile',
            scope_note: 'host_profile',
            detail: { enabled: false, inner: {} },
          },
        ],
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Disabled for the host surface')).toBeInTheDocument()
    // The unqualified claim must be gone, not merely supplemented.
    expect(screen.queryByText('Disabled by policy')).not.toBeInTheDocument()
  })

  it('still labels a policy-wide capability pin as policy-disabled', async () => {
    // The caveat must not over-apply: a Level-1 ceiling DOES bind every surface.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        scopes: [
          {
            scope: 'capabilities.cron',
            archetype: 'capability',
            governed: true,
            source: 'policy',
            scope_note: 'policy_wide',
            detail: { enabled: false, inner: {} },
          },
        ],
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Disabled by policy')).toBeInTheDocument()
    expect(screen.queryByText('Disabled for the host surface')).not.toBeInTheDocument()
  })

  it('names the surfaces that carry their own profile', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ other_bound_surfaces: ['cron', 'subagent'] }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    // Joined via `fmtList` (Intl.ListFormat), not a hardcoded ', ' — zh joins
    // with 、 and no spaces, so a literal separator would render wrong there.
    expect(await screen.findByText(/cron and subagent/)).toBeInTheDocument()
  })

  it('omits the surfaces footnote when no other surface is bound', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ other_bound_surfaces: [] }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    await screen.findByText('Policy v1')
    expect(
      screen.queryByText(/A surface with its own profile can allow what the host cannot/),
    ).not.toBeInTheDocument()
  })

  it('shows a warning banner naming the unusable profiles', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ fallback_profiles: ['host'] }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Fallback profile in effect')).toBeInTheDocument()
    expect(screen.getByText(/policy fallback ceiling/)).toBeInTheDocument()
    expect(screen.getByText(/Affected: host\./)).toBeInTheDocument()
    // The remedy is only useful if it says WHERE the file is.
    expect(screen.getByText(/profiles folder of your Kiro Crew data home/)).toBeInTheDocument()
    // Causes and the restart caveat live on the demoted line, not in the body.
    expect(screen.getByText(/extends a profile that is missing/)).toBeInTheDocument()
  })

  it('names every unusable profile, not just the host one', async () => {
    // The reason the contract is a list: a broken sibling surface deny-alls
    // itself just as silently, and a host-only boolean could not report it.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ fallback_profiles: ['cron', 'subagent'] }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Fallback profile in effect')).toBeInTheDocument()
    expect(screen.getByText(/Affected: cron, subagent\./)).toBeInTheDocument()
  })

  it('does not show fallback banner when fallback_profiles is empty', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ fallback_profiles: [] }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    await screen.findByText('Policy v1')
    expect(screen.queryByText('Fallback profile in effect')).not.toBeInTheDocument()
  })

  it('does not show fallback banner when fallback_profiles is absent', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govGoverned())
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    await screen.findByText('Policy v1')
    expect(screen.queryByText('Fallback profile in effect')).not.toBeInTheDocument()
  })

  it('lists unknown companion scopes per affected profile', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        unknown_profile_scopes: {
          host: ['capabilities.board', 'capabilities.channels'],
          cron: ['capabilities.board'],
        },
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Profile scopes not in this build')).toBeInTheDocument()
    // One row per profile stem, each naming its own skipped scopes.
    expect(screen.getByText('host')).toBeInTheDocument()
    expect(screen.getByText('cron')).toBeInTheDocument()
    // Joined via fmtList (Intl.ListFormat unit type), same contract as the
    // fallback banner: en joins with ', ', zh with 、 and no spaces.
    expect(screen.getByText('capabilities.board, capabilities.channels')).toBeInTheDocument()
    // Rows are sorted with compareText: the fixture feeds insertion order
    // host→cron, so an unsorted render would show host first. Node.compareDocumentPosition
    // pins the DOM order the comparator produces.
    const cron = screen.getByText('cron')
    const host = screen.getByText('host')
    expect(cron.compareDocumentPosition(host) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The explanation is a tooltip, not body copy: it must NOT be in the
    // document until the InfoTip is opened.
    expect(screen.queryByText(/companion edition/)).not.toBeInTheDocument()
  })

  it('explains companion scopes in the InfoTip on demand', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ unknown_profile_scopes: { host: ['capabilities.board'] } }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    const block = (await screen.findByText('Profile scopes not in this build')).parentElement!
    fireEvent.click(within(block).getByRole('button', { name: /more information/i }))
    expect(
      await screen.findByText(/this build does not register.*no effect in this build/),
    ).toBeInTheDocument()
  })

  it('renders unknown scopes stacked with the fallback banner they are siblings of', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        fallback_profiles: ['cron'],
        unknown_profile_scopes: { host: ['capabilities.board'] },
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    // Both sibling diagnostics render together — one must not suppress the other.
    expect(await screen.findByText('Fallback profile in effect')).toBeInTheDocument()
    expect(screen.getByText('Profile scopes not in this build')).toBeInTheDocument()
  })

  it('shows unknown scopes even with no policy and no host profile', async () => {
    // The payload aggregates EVERY loaded profile, so a standalone install with
    // only a companion-edition subagent.json is a valid producer state. The
    // no-policy card must yield to the diagnostic rather than swallow it.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govNoPolicy({ unknown_profile_scopes: { subagent: ['capabilities.board'] } }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Profile scopes not in this build')).toBeInTheDocument()
    expect(screen.getByText('subagent')).toBeInTheDocument()
    expect(screen.queryByText('No enterprise policy in effect')).not.toBeInTheDocument()
  })

  it('renders no unknown-scopes block when the field is an empty object', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ unknown_profile_scopes: {} }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    await screen.findByText('Policy v1')
    expect(screen.queryByText('Profile scopes not in this build')).not.toBeInTheDocument()
  })

  it('renders no row for a profile whose scope list is empty', async () => {
    // The producer contract says an empty list cannot happen; the filter makes
    // the UI true independent of it — no badge with nothing after it.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ unknown_profile_scopes: { host: [] } }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    await screen.findByText('Policy v1')
    expect(screen.queryByText('Profile scopes not in this build')).not.toBeInTheDocument()
  })

  it('renders no unknown-scopes block when the field is absent', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govGoverned())
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    await screen.findByText('Policy v1')
    expect(screen.queryByText('Profile scopes not in this build')).not.toBeInTheDocument()
  })
})

describe('SecurityPanel — central policy distribution', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
    ;(api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mockResolvedValue(CONSENT_NONE)
  })

  it('reports the transport, the poller, the cache age and the last refresh', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({ distribution: govDistribution() }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Central policy distribution')).toBeInTheDocument()
    // The SCHEME, never the URL: the producer withholds the endpoint, so a test that
    // ever finds one here is reporting a leak of the fleet's control plane.
    expect(screen.getByText('Source: https')).toBeInTheDocument()
    // Durations go through fmtDuration, so assert the copy plus the magnitude rather
    // than a unit spelling that is locale-owned.
    expect(screen.getByText(/Refreshed every/).textContent).toMatch(/15/)
    expect(screen.getByText(/Cached copy is/).textContent).toMatch(/2/)
    expect(screen.getByText('Last refresh found no change')).toBeInTheDocument()
    // A governed host has its ceiling, so the pending caveat must stay off.
    expect(screen.queryByText(/enterprise ceiling restricts the host surface/)).not.toBeInTheDocument()
  })

  // The trap this block exists to close: `has_policy: false` + `profile: null` is
  // BOTH "standalone, nothing governs this machine" and "centrally governed, the
  // first fetch has not landed". Rendering the reassuring card for the second is
  // telling an operator their ceiling is off when it is merely late.
  it('does not claim "no enterprise policy" on a host whose first fetch has not landed', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govNoPolicy({
        distribution: govDistribution({
          cache_present: false,
          cache_age_seconds: null,
          last_refresh_status: '',
          last_refresh_age_seconds: null,
        }),
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Central policy distribution')).toBeInTheDocument()
    expect(screen.getByText(/enterprise ceiling restricts the host surface/)).toBeInTheDocument()
    // Not merely supplemented — the reassurance must be GONE.
    expect(screen.queryByText('No enterprise policy in effect')).not.toBeInTheDocument()
    // And the scope rows render in its place, so the page still says what is enforced.
    expect(screen.getAllByText('Not restricted').length).toBeGreaterThan(0)
  })

  it('names the cache age when the copy is days old', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        distribution: govDistribution({
          cache_age_seconds: 3 * 86400,
          last_refresh_status: 'unreachable',
          last_refresh_age_seconds: 60,
        }),
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    // dropZero is what keeps three days from rendering as "3d 0h 0m".
    const cache = await screen.findByText(/Cached copy is/)
    expect(cache.textContent).toMatch(/3\s* ?d/)
    expect(cache.textContent).not.toMatch(/0/)
    expect(screen.getByText('Source was unreachable on the last refresh')).toBeInTheDocument()
  })

  it('reports a misconfigured declaration even though it is not "configured"', async () => {
    // The producer answers `configured: false` with an error code: the fleet DID
    // point this host somewhere, the pins just do not parse. Gating the block on
    // `configured` alone would drop the one state that most needs saying.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govNoPolicy({ distribution: { configured: false, error_code: 'misconfigured' } }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Distribution settings could not be read')).toBeInTheDocument()
    expect(screen.queryByText('No enterprise policy in effect')).not.toBeInTheDocument()
    // Nothing is known about the transport or the cache, so no badge claims one.
    expect(screen.queryByText(/Source:/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Cached copy/)).not.toBeInTheDocument()
  })

  it('survives the unavailable short-circuit that hides every other governance fact', async () => {
    // `unavailable` collapses the whole viewer to one soft notice, but the fail-safe
    // snapshot still carries the distribution posture — and a stopped poller plus an
    // unreachable source is exactly what an operator needs at that moment.
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govNoPolicy({
        unavailable: true,
        distribution: govDistribution({ refresher_running: false, last_refresh_status: 'unreachable' }),
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText(/Governance status is temporarily unavailable/)).toBeInTheDocument()
    expect(screen.getByText('Central policy distribution')).toBeInTheDocument()
    expect(screen.getByText(/Background refresh is not running/)).toBeInTheDocument()
    expect(screen.getByText('Source was unreachable on the last refresh')).toBeInTheDocument()
    // The pending caveat must NOT fire here: the fail-safe snapshot reports
    // `has_policy: false` because it could not resolve the ceiling, not because there
    // is none, so reading it as pending would announce an unrestricted host surface on
    // a machine whose ceiling is installed and enforcing.
    expect(screen.queryByText(/enterprise ceiling restricts the host surface/)).not.toBeInTheDocument()
  })

  it('says a boot-only fetch is boot-only rather than calling the poller stopped', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govGoverned({
        distribution: govDistribution({ refresh_interval_seconds: 0, refresher_running: false }),
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('Fetched at startup only')).toBeInTheDocument()
    expect(screen.queryByText('Background refresh is not running')).not.toBeInTheDocument()
  })

  it('renders no block, and no empty section, when the payload omits distribution', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('No enterprise policy in effect')).toBeInTheDocument()
    expect(screen.queryByText('Central policy distribution')).not.toBeInTheDocument()
  })

  it('renders no block when a build without the feature reports configured: false', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      govNoPolicy({ distribution: { configured: false } }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=governance' })

    expect(await screen.findByText('No enterprise policy in effect')).toBeInTheDocument()
    expect(screen.queryByText('Central policy distribution')).not.toBeInTheDocument()
  })
})

describe('SecurityPanel — posture disclosure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
    ;(api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mockResolvedValue(CONSENT_NONE)
  })

  it('renders a pill per control using the server-derived count and unit', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    // The whole point of the change: the count is data, not a hardcoded literal.
    expect(await screen.findByText('2 output paths')).toBeInTheDocument()
    expect(screen.getByText('30 credential paths')).toBeInTheDocument()
  })

  it('items are hidden until the row is expanded, then reveal label + detail', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })
    await screen.findByText('2 output paths')

    expect(screen.queryByText('Dashboard live stream')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(/^Show Output redaction details/))

    expect(await screen.findByText('Dashboard live stream')).toBeInTheDocument()
    expect(screen.getByText('chat_runner.py — StreamRedactor')).toBeInTheDocument()

    // Collapsing flips the row back to closed. Asserted via aria-expanded rather
    // than DOM removal: AnimatePresence keeps the exiting subtree mounted until
    // its exit transition finishes, which framer-motion does not drive to
    // completion under the test environment's faked rAF.
    fireEvent.click(screen.getByLabelText(/^Hide Output redaction details/))
    await waitFor(() =>
      expect(screen.getByLabelText(/^Show Output redaction details/)).toHaveAttribute(
        'aria-expanded',
        'false',
      ),
    )
  })

  it('a long list is truncated with a "Show N more" affordance and is filterable', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })
    await screen.findByText('30 credential paths')
    fireEvent.click(screen.getByLabelText(/^Show Sensitive path blocking details/))

    // 30 items > INITIAL_VISIBLE (25) → the tail is behind one click.
    expect(await screen.findByText('~/.secret-0')).toBeInTheDocument()
    expect(screen.queryByText('~/.secret-29')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show 5 more' }))
    expect(screen.getByText('~/.secret-29')).toBeInTheDocument()

    // The filter matches on detail text too, not just the label.
    fireEvent.change(screen.getByLabelText('Filter Sensitive path blocking'), {
      target: { value: 'needle' },
    })
    expect(screen.getByText('~/.secret-7')).toBeInTheDocument()
    expect(screen.queryByText('~/.secret-0')).not.toBeInTheDocument()
  })

  it('a short list gets no filter box', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })
    await screen.findByText('2 output paths')
    fireEvent.click(screen.getByLabelText(/^Show Output redaction details/))

    await screen.findByText('Dashboard live stream')
    expect(screen.queryByLabelText('Filter Output redaction')).not.toBeInTheDocument()
  })

  it('an unavailable control reads "unavailable", never a misleading zero', async () => {
    // Regression guard: rendering `count` directly would print "0 output paths"
    // and tell the operator a live control covers nothing.
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(
      posture({
        controls: [
          {
            key: 'redaction_paths',
            label: 'Output redaction',
            unit: 'output paths',
            summary: '',
            source: '',
            count: null,
            items: [],
            unavailable: true,
          },
        ],
        counts: { redaction_paths: null },
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    expect(await screen.findByText('unavailable')).toBeInTheDocument()
    expect(screen.queryByText('0 output paths')).not.toBeInTheDocument()
  })

  it('shows a soft notice when the posture endpoint fails', async () => {
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    expect(
      await screen.findByText(/Security posture detail is temporarily unavailable/),
    ).toBeInTheDocument()
  })

  it('does NOT paint "unavailable" on the deny gate while denied-commands is still loading', async () => {
    // While denied-commands is still loading, the deny gate must NOT paint
    // "unavailable": `dc?.effective_count ?? null` is null before the second
    // query resolves, and a fully-enforced 137-rule gate rendering as an amber
    // "unavailable" warning is the exact misleading-security-signal failure the
    // governance viewer's soft notice exists to prevent. The two queries resolve
    // independently, so posture-first is a normal interleaving.
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    // Posture resolved; denied-commands never will.
    expect(await screen.findByText('137 built-in rules')).toBeInTheDocument()
    expect(screen.queryByText('unavailable')).not.toBeInTheDocument()
  })

  it('a FAILED denied-commands query reads "unavailable", not the shipped total', async () => {
    // The loading case correctly falls back to the shipped total (still honest —
    // just not yet narrowed by opt-outs). An ERROR must not: the query has stopped
    // retrying, so reporting the shipped total would claim rules the user disabled
    // are enforced, indefinitely. Over-reporting a security control is the worse
    // direction, so this state is explicitly "unavailable".
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    // Another control still resolves, proving the panel itself is fine.
    expect(await screen.findByText('2 output paths')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('unavailable')).toBeInTheDocument())
    expect(screen.queryByText('137 built-in rules')).not.toBeInTheDocument()
  })

  it('the deny pill counts built-ins only, so a custom rule cannot exceed the total', async () => {
    // The row counts built-ins only, not `dc.effective_count` (builtins +
    // user_added): folding user rules in would render the nonsense ratio
    // "138 of 137 built-in rules" against a built-in-only denominator.
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(
      snapshot({
        // Both builtins in the fixture are enabled; effective_count adds the 1 user rule.
        effective_count: 3,
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    expect(await screen.findByText('2 built-in rules')).toBeInTheDocument()
    expect(screen.queryByText('3 built-in rules')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(/^Show Denied commands details/))
    expect(
      await screen.findByText(/2 of 2 built-in rules are currently enforced/),
    ).toBeInTheDocument()
    // Custom patterns are accounted for, just not folded into the built-in ratio.
    expect(
      screen.getByText(/Your custom patterns are counted separately in Denied Commands/),
    ).toBeInTheDocument()
  })

  it('the deny pill shows enabled built-ins, not the shipped rule total', async () => {
    // The posture registry reports the SHIPPED built-in table; the pill must show
    // what is actually enforced after opt-outs + policy pins. Counted from
    // `dc.builtins` (1 of the 2 fixture rules disabled → 1), NOT `effective_count`,
    // which also includes user_added and would overshoot the denominator.
    //
    // Lives here rather than with the rules section because the pill is a POSTURE
    // row: the two sections share the `denied-commands` query key, which is what
    // lets this count stay effective-accurate without the rules pane mounted.
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(
      snapshot({
        builtins: snapshot().builtins.map((b, i) => (i === 0 ? { ...b, enabled: false } : b)),
        effective_count: 129,
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    expect(await screen.findByText('1 built-in rules')).toBeInTheDocument()
    expect(screen.queryByText('137 built-in rules')).not.toBeInTheDocument()
    expect(screen.queryByText('129 built-in rules')).not.toBeInTheDocument()
  })

  it('status rows reserve the external-link slot so every badge shares one right edge', async () => {
    // The hover-only ExternalLink slot is reserved on every row, linked or not,
    // so all badges share one right edge; rendering it only on rows with an
    // href would push those badges left of the unlinked rows' badges.
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    // 'Standard' (Process Sandbox) is linked; 'Interactive' (Tool Approval) is not.
    // Both are also rail summaries, so scope to the posture card's own rows.
    for (const text of ['Standard', 'Interactive']) {
      const trailing = (await screen.findAllByText(text)).at(-1)!.parentElement
      expect(trailing?.children).toHaveLength(2)
    }
  })

  it('clearing a filter re-applies the truncation cap', async () => {
    // `expanded` must reset on a filter change: otherwise expanding a FILTERED
    // subset and then clearing the filter renders the whole list, defeating the
    // INITIAL_VISIBLE DOM cap. Must expand *while filtered* to exercise it.
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })
    await screen.findByText('30 credential paths')
    fireEvent.click(screen.getByLabelText(/^Show Sensitive path blocking details/))

    const filterBox = await screen.findByLabelText('Filter Sensitive path blocking')
    // 'Third-party' matches 29 of 30 (the needle has a different detail), so the
    // filtered set is still over the 25-item cap and has its own "Show 4 more".
    fireEvent.change(filterBox, { target: { value: 'Third-party' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Show 4 more' }))
    expect(screen.getByText('~/.secret-29')).toBeInTheDocument()

    fireEvent.change(filterBox, { target: { value: '' } })
    // Back to all 30 → truncated again rather than dumping every row.
    expect(await screen.findByRole('button', { name: 'Show 5 more' })).toBeInTheDocument()
    expect(screen.queryByText('~/.secret-29')).not.toBeInTheDocument()
  })

  it('the header announces the count, and an unavailable control is not a disabled button', async () => {
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(
      posture({
        controls: [
          {
            key: 'redaction_paths', label: 'Output redaction', unit: 'output paths',
            summary: '', source: '', count: null, items: [], unavailable: true,
          },
          {
            key: 'audit_surfaces', label: 'SEL audit logging', unit: 'audited surfaces',
            summary: '', source: '', count: 2, unavailable: false,
            items: [{ label: 'Slack handler', detail: '' }, { label: 'MCP core', detail: '' }],
          },
        ],
        counts: { redaction_paths: null, audit_surfaces: 2 },
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    // The badge is the row's payload, so it belongs in the accessible name.
    expect(
      await screen.findByLabelText('Show SEL audit logging details — 2 audited surfaces'),
    ).toBeInTheDocument()
    // A control with nothing to expand is a plain row, not an aria-disabled button.
    expect(screen.getByText('unavailable')).toBeInTheDocument()
    expect(screen.queryByLabelText(/Output redaction details/)).not.toBeInTheDocument()
  })

  it('renders a control the frontend has no icon for (forward compatibility)', async () => {
    // A backend that registers a new control must not have its row silently
    // dropped just because POSTURE_ICONS has no entry for the key yet.
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(
      posture({
        controls: [
          {
            key: 'brand_new_control',
            label: 'Brand new control',
            unit: 'widgets',
            summary: '',
            source: '',
            count: 3,
            items: [{ label: 'thing', detail: '' }],
            unavailable: false,
          },
        ],
        counts: { brand_new_control: 3 },
      }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    expect(await screen.findByText('Brand new control')).toBeInTheDocument()
    expect(screen.getByText('3 widgets')).toBeInTheDocument()
  })
})

/* ── Third-party app execution toggle ──────────────────────────────────────
 * The blanket admission gate for app code that is not a shipped builtin
 * (`agent.apps_allow_third_party`). Two properties matter enough to pin:
 *   1. the switch writes a real JSON boolean, because the backend's
 *      `third_party_execution_allowed()` admits ONLY `true` by identity;
 *   2. a config value that is truthy but NOT that boolean must render OFF —
 *      showing "on" for a value the gate rejects would tell the user their
 *      apps are admitted when every execution decision still denies them.
 */

describe('SecurityPanel — trusted third-party apps', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue(trusted())
    ;(api.setTrustAllApps as ReturnType<typeof vi.fn>).mockResolvedValue(trusted({ allowAll: true }))
    ;(api.untrustApp as ReturnType<typeof vi.fn>).mockResolvedValue({
      apps: ['oncall-radar'],
      ineffective: [],
      allowAll: false,
      disabled: false,
    })
  })

  it('renders one row per granted app, each with a trusted badge and a Revoke action', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('trusted-app-launchdarkly')
    expect(within(row).getByText('launchdarkly')).toBeInTheDocument()
    expect(within(row).getByText(T.trustedBadge())).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: T.revoke() })).toBeInTheDocument()
    // Both grants render — the list is not truncated to the first.
    expect(screen.getByTestId('trusted-app-oncall-radar')).toBeInTheDocument()
    // With grants present, the empty state must NOT also be on screen.
    expect(screen.queryByText(T.empty())).not.toBeInTheDocument()
  })

  it('Revoke confirms BEFORE mutating — the app stops working, so say so first', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('trusted-app-launchdarkly')
    fireEvent.click(within(row).getByRole('button', { name: T.revoke() }))

    // Nothing is revoked on the first click: a first-run reviewer flagged
    // discovering "this also disables the app" AFTER the click as a blocker.
    expect(api.untrustApp).not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(T.revokeConfirmBody('launchdarkly'))).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: T.revokeConfirmOk() }))
    await waitFor(() => expect(api.untrustApp).toHaveBeenCalledWith('launchdarkly'))
  })

  it('Revoke needs no acknowledgement checkbox — it tightens, not weakens', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('trusted-app-launchdarkly')
    fireEvent.click(within(row).getByRole('button', { name: T.revoke() }))

    const dialog = await screen.findByRole('dialog')
    // Demanding "I understand this weakens protection" for the SAFE direction
    // trains people to tick without reading, which is what makes the checkbox
    // worthless on the dangerous direction (allow-all).
    expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: T.revokeConfirmOk() })).toBeEnabled()
  })

  it('cancelling the revoke confirm mutates nothing', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('trusted-app-launchdarkly')
    fireEvent.click(within(row).getByRole('button', { name: T.revoke() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: T.cancel() }))

    expect(api.untrustApp).not.toHaveBeenCalled()
  })

  it('surfaces the also-disabled notice when the revoke response says disabled', async () => {
    ;(api.untrustApp as ReturnType<typeof vi.fn>).mockResolvedValue({
      apps: ['oncall-radar'],
      ineffective: [],
      allowAll: false,
      disabled: true,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('trusted-app-launchdarkly')
    // The notice is a consequence of the response, so it is absent beforehand.
    expect(screen.queryByText(T.revokeDisables('launchdarkly'))).not.toBeInTheDocument()
    fireEvent.click(within(row).getByRole('button', { name: T.revoke() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: T.revokeConfirmOk() }))

    expect(await screen.findByText(T.revokeDisables('launchdarkly'))).toBeInTheDocument()
  })

  it('does NOT surface the also-disabled notice when disabled is false', async () => {
    // Revoking trust on an already-disabled app changes nothing about whether it
    // runs, so claiming "we also disabled it" would be a false statement.
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('trusted-app-launchdarkly')
    fireEvent.click(within(row).getByRole('button', { name: T.revoke() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: T.revokeConfirmOk() }))

    await waitFor(() => expect(api.untrustApp).toHaveBeenCalledWith('launchdarkly'))
    expect(screen.queryByText(T.revokeDisables('launchdarkly'))).not.toBeInTheDocument()
  })

  it('turning allow-all ON requires the acknowledgement before it mutates', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    // Wait for the snapshot to land before clicking: the toggle is rendered
    // (and findable) while `listTrustedApps` is still in flight, but DISABLED
    // until it resolves, so an early click is silently swallowed.
    await screen.findByTestId('trusted-app-launchdarkly')
    const toggle = screen.getByRole('switch', { name: T.allowAllLabel() })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(toggle)
    // Widening what un-reviewed third-party code may do must not be one click.
    expect(api.setTrustAllApps).not.toHaveBeenCalled()

    const dialog = await screen.findByRole('dialog')
    const confirmBtn = within(dialog).getByRole('button', { name: T.confirmBtn() })
    expect(confirmBtn).toBeDisabled()
    // Clicking while un-acked is a no-op.
    fireEvent.click(confirmBtn)
    expect(api.setTrustAllApps).not.toHaveBeenCalled()

    fireEvent.click(screen.getByLabelText(T.allowAllAck()))
    fireEvent.click(within(dialog).getByRole('button', { name: T.confirmBtn() }))
    await waitFor(() => expect(api.setTrustAllApps).toHaveBeenCalledWith(true))
  })

  it('turning allow-all OFF is immediate (no modal)', async () => {
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue(trusted({ allowAll: true }))
    ;(api.setTrustAllApps as ReturnType<typeof vi.fn>).mockResolvedValue(trusted({ allowAll: false }))
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    await screen.findByTestId('trusted-app-launchdarkly')
    const toggle = screen.getByRole('switch', { name: T.allowAllLabel() })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)

    await waitFor(() => expect(api.setTrustAllApps).toHaveBeenCalledWith(false))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders the empty state when no app is granted', async () => {
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue(trusted({ apps: [] }))
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    expect(await screen.findByText(T.empty())).toBeInTheDocument()
    expect(screen.queryByText(T.trustedBadge())).not.toBeInTheDocument()
    // The allow-all lever is still offered with zero grants.
    expect(screen.getByRole('switch', { name: T.allowAllLabel() })).toBeInTheDocument()
  })

  /**
   * Stored-but-unenforced grants.
   *
   * `trusted_app_names` (the enforcement reader) requires the app-name charset,
   * so a hand-edited config.json can hold entries the gate silently ignores.
   * Folding them into the granted list claimed trust that does not exist and
   * left the user no way to see why their app was still blocked.
   */
  it('renders ineffective entries in their own group, distinct from real grants', async () => {
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue(
      trusted({ ineffective: INEFFECTIVE }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const group = await screen.findByTestId('trusted-apps-ineffective')
    // The group says what these entries are and why they do nothing.
    expect(within(group).getByText(T.ineffectiveLabel())).toBeInTheDocument()
    expect(within(group).getByText(T.ineffectiveDescription())).toBeInTheDocument()

    for (const name of INEFFECTIVE) {
      const row = within(group).getByTestId(`ineffective-app-${name}`)
      expect(within(row).getByText(name)).toBeInTheDocument()
      // Visually distinguished from an effective grant: struck through, and
      // WITHOUT the "trusted" badge that marks an enforced grant.
      expect(row.querySelector('code')).toHaveClass('line-through')
      expect(within(row).queryByText(T.trustedBadge())).not.toBeInTheDocument()
    }

    // An effective grant stays in the granted list, outside this group, and keeps
    // its badge — the two populations never merge.
    const granted = screen.getByTestId('trusted-app-launchdarkly')
    expect(group.contains(granted)).toBe(false)
    expect(within(granted).getByText(T.trustedBadge())).toBeInTheDocument()
    expect(granted.querySelector('code')).not.toHaveClass('line-through')
  })

  it('Revoke works on an ineffective entry — junk can be cleared out', async () => {
    // The revoke endpoint deliberately does NOT validate the name being removed,
    // precisely so an entry that can never be granted can still be deleted.
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue(
      trusted({ ineffective: INEFFECTIVE }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    const row = await screen.findByTestId('ineffective-app-LD-App')
    fireEvent.click(within(row).getByRole('button', { name: T.revoke() }))

    await waitFor(() => expect(api.untrustApp).toHaveBeenCalledWith('LD-App'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('omits the ineffective group entirely when every stored entry is enforced', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    await screen.findByTestId('trusted-app-launchdarkly')
    expect(screen.queryByTestId('trusted-apps-ineffective')).not.toBeInTheDocument()
    expect(screen.queryByText(T.ineffectiveLabel())).not.toBeInTheDocument()
  })

  it('shows the ineffective group even when no grant is effective', async () => {
    // `apps: []` with junk stored is the exact case the split exists for: the
    // empty state is TRUE (nothing is trusted) and the group explains the entries
    // the user can see in their config.
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue(
      trusted({ apps: [], ineffective: INEFFECTIVE }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    expect(await screen.findByText(T.empty())).toBeInTheDocument()
    const group = screen.getByTestId('trusted-apps-ineffective')
    expect(within(group).getByTestId('ineffective-app-LD-App')).toBeInTheDocument()
  })

  it('the allow-all row carries a data-setting-label so the App Store can deep-link it', async () => {
    // `?tab=security&highlight=<id>` resolves an id to a LABEL via SETTINGS_REGISTRY
    // and finds the row by this attribute (see hooks/useSettingHighlight.ts). Without
    // it the link lands on the tab and highlights nothing.
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })

    await screen.findByRole('switch', { name: T.allowAllLabel() })
    expect(
      document.querySelector(`[data-setting-label="${T.allowAllLabel()}"]`),
    ).not.toBeNull()
  })
})

describe('trustFailureMessage', () => {
  // REGRESSION: both new 409s carry the only actionable detail in the body's
  // `error` — which file to edit (`config.local.json` owns the setting) or which
  // apps are still executing after trust was withdrawn. Collapsing them into a
  // generic message would put the UI back to reporting a change that did not
  // happen as though it had.
  it('prefers the backend detail over the mapped status message', () => {
    const err = new ApiError(
      409,
      'Conflict',
      JSON.stringify({
        error: 'apps_trusted is set in /home/u/.kiro/crew/config.local.json',
        code: 'trust_setting_overlay_owned',
      }),
    )
    expect(trustFailureMessage(err)).toContain('config.local.json')
  })

  it('falls back to the mapped message when the body is not JSON', () => {
    expect(trustFailureMessage(new ApiError(500, 'Server error', '<html>502</html>')))
      .toBe('Server error')
  })

  it('handles a non-ApiError rejection without throwing', () => {
    expect(trustFailureMessage(new Error('network down'))).toBe('network down')
    expect(trustFailureMessage('nope')).toBe('unknown error')
  })
})

/* ── Properties inherited from the standalone card #1414 added ───────────────
 * That card owned `agent.apps_allow_third_party` through the generic config
 * PATCH, which performs no teardown — switching it off left every app it had
 * admitted still executing. The two controls were consolidated into the one
 * wired to `PUT /api/security/trusted-apps/allow-all`, which sweeps. These pin
 * the properties worth carrying over, so the consolidation cannot quietly lose
 * them.
 */
describe('SecurityPanel — allow-all toggle inherits #1414 semantics', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
  })

  it('shows the blanket-trust warning only while allow-all is on', async () => {
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue({
      apps: [], ineffective: [], allowAll: true,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })
    // The cost of the blanket flag — every third-party app, including future
    // installs — must be stated where the switch is, not left implicit.
    expect(await screen.findByText(/trusts every third-party app/i)).toBeInTheDocument()
  })

  it('a truthy non-boolean allowAll renders OFF, matching the backend gate', async () => {
    // `third_party_execution_allowed()` admits ONLY the literal boolean by
    // identity, so a hand-edited "true" grants nothing. Rendering it as ON would
    // tell the user their apps are admitted while every decision still denies.
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockResolvedValue({
      apps: [], ineffective: [], allowAll: 'true' as unknown as boolean,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })
    const sw = await screen.findByRole('switch', { name: /trust every third-party app/i })
    expect(sw).toHaveAttribute('aria-checked', 'false')
  })

  it('a FAILED trusted-apps read renders no switch and says so', async () => {
    // UNKNOWN is not OFF. `role="switch"` has no unknown state (aria-checked
    // `mixed` is checkbox-only), so a switch here would assert a state we could
    // not read — and a click would write `true` onto a possibly-already-true
    // setting instead of revoking it.
    ;(api.listTrustedApps as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=apps' })
    expect(await screen.findByText(/could not read the current setting/i)).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: /trust every third-party app/i })).toBeNull()
  })
})
