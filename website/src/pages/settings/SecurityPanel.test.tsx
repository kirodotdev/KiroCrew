import { ApiError } from '../../api/client'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { renderWithProviders, createTestStore } from '../../test/helpers'
import type { DeniedCommandsData } from '../../api/client'

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
    listTrustedApps: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    setTrustAllApps: vi.fn(),
  },
}))

import { api } from '../../api/client'
import type { GovernancePolicyData, SecurityPostureData, TrustedAppsData } from '../../api/client'
import { SecurityPanel, trustFailureMessage } from './SecurityPanel'
import { i18nT } from '../../i18n/t'

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

const PINNED_DESC = 'Blocks EC2 instance termination'
const TOGGLE_DESC = 'Blocks CloudFormation stack deletion'
const USER_PATTERN = 'rm -rf /tmp/mine'

/** Default payload for the rail's tailnet read.
 *
 * Every `beforeEach` in this file resolves `api.tailnetStatus` with this. It must
 * RESOLVE rather than merely exist: a bare `vi.fn()` returns undefined, which
 * react-query rejects ("Query data cannot be undefined") and reports once per
 * test, since the rail reads it on mount in all of them. `off` is the right
 * default here — this file covers the rest of the panel, and the section's own
 * states are covered in SecurityPanel.tailnet.test.tsx.
 */
const TAILNET_OFF = {
  enabled: false,
  governance_pinned: false,
  host: '',
  origin: '',
  resolved_at: 0,
  state: 'off',
} as const

function snapshot(overrides: Partial<DeniedCommandsData> = {}): DeniedCommandsData {
  return {
    builtins: [
      {
        id: 'aws-destructive-cfn-delete-stack',
        pattern: 'aws.*cloudformation.*delete-stack.*',
        category: 'aws-destructive',
        description: TOGGLE_DESC,
        enabled: true,
        pinned: false,
      },
      {
        id: 'aws-destructive-ec2-terminate-instances',
        pattern: 'aws.*ec2.*terminate-instances.*',
        category: 'aws-destructive',
        description: PINNED_DESC,
        enabled: true,
        pinned: true,
      },
    ],
    user_added: [
      { id: 'user-1', pattern: USER_PATTERN, enabled: true },
    ],
    disable_all: false,
    effective_count: 129,
    governance_locked: false,
    ...overrides,
  }
}

/** Render the panel with the denied-commands query pre-resolved.
 *
 * Built-in rules live inside per-category accordions that are COLLAPSED by
 * default, so the rows are not in the DOM until a category is expanded. After
 * the query hydrates, click "Expand all" so every rule row is present for the
 * assertions below (mirrors what a user does to reach an individual rule). */
async function renderPanel(data: DeniedCommandsData = snapshot()) {
  ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
  // Wait for the async query to hydrate the category accordion, then expand all.
  const expandAll = await screen.findByRole('button', { name: 'Expand all' })
  fireEvent.click(expandAll)
  await screen.findByLabelText(TOGGLE_DESC)
  return utils
}

/** A posture snapshot with one short control (no filter box) and one long enough
 *  to trigger both the filter box and the "Show N more" truncation. */
function posture(overrides: Partial<SecurityPostureData> = {}): SecurityPostureData {
  const many = Array.from({ length: 30 }, (_, i) => ({
    label: `~/.secret-${i}`,
    detail: i === 7 ? 'Needle detail' : 'Third-party credential store',
  }))
  return {
    controls: [
      {
        key: 'redaction_paths',
        label: 'Output redaction',
        unit: 'output paths',
        summary: 'Every boundary where agent output reaches a human.',
        source: 'src/kiro_crew/security.py',
        count: 2,
        items: [
          { label: 'Dashboard live stream', detail: 'chat_runner.py — StreamRedactor' },
          { label: 'Slack messages', detail: 'handler.py — final pass' },
        ],
        unavailable: false,
      },
      {
        key: 'sensitive_paths',
        label: 'Sensitive path blocking',
        unit: 'credential paths',
        summary: 'Paths the agent cannot read or write.',
        source: 'src/kiro_crew/security.py',
        count: many.length,
        items: many,
        unavailable: false,
      },
      {
        key: 'denied_commands',
        label: 'Denied commands',
        unit: 'built-in rules',
        summary: 'Destructive shell operations blocked at the gate.',
        source: 'src/kiro_crew/security.py',
        count: 137,
        items: [{ label: 'Blocks stack deletion', detail: 'aws-destructive' }],
        unavailable: false,
      },
    ],
    counts: { redaction_paths: 2, sensitive_paths: 30, denied_commands: 137 },
    ...overrides,
  }
}

/** No-policy (standalone) governance snapshot: every scope ungoverned. */
function govNoPolicy(overrides: Partial<GovernancePolicyData> = {}): GovernancePolicyData {
  return {
    version: null,
    has_policy: false,
    profile: null,
    unavailable: false,
    scopes: [
      { scope: 'tools', archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} },
      { scope: 'commands', archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} },
    ],
    ...overrides,
  }
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

describe('SecurityPanel — denied commands', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.toggleBuiltinDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.setDeniedCommandsDisableAll as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.addUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.toggleUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.deleteUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  it('toggling a built-in OFF opens the confirm modal and only mutates after ack', async () => {
    await renderPanel()

    // Turning a built-in OFF must NOT mutate immediately — it opens the modal.
    fireEvent.click(screen.getByRole('switch', { name: TOGGLE_DESC }))
    expect(api.toggleBuiltinDeniedCommand).not.toHaveBeenCalled()

    // Modal is open; the Disable button is gated on the ack checkbox.
    const dialog = await screen.findByRole('dialog')
    const disableBtn = within(dialog).getByRole('button', { name: 'Disable' })
    expect(disableBtn).toBeDisabled()

    // Clicking Disable while un-acked is a no-op.
    fireEvent.click(disableBtn)
    expect(api.toggleBuiltinDeniedCommand).not.toHaveBeenCalled()

    // Ack the warning, then Disable → the mutation fires with enabled=false.
    fireEvent.click(
      screen.getByLabelText("I understand this weakens Kiro Crew's protection."),
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }))
    await waitFor(() =>
      expect(api.toggleBuiltinDeniedCommand).toHaveBeenCalledWith(
        'aws-destructive-cfn-delete-stack',
        false,
      ),
    )
  })

  it('toggling a built-in ON is immediate (no modal)', async () => {
    await renderPanel(
      snapshot({
        builtins: [
          {
            id: 'aws-destructive-cfn-delete-stack',
            pattern: 'aws.*cloudformation.*delete-stack.*',
            category: 'aws-destructive',
            description: TOGGLE_DESC,
            enabled: false,
            pinned: false,
          },
        ],
        user_added: [],
      }),
    )

    fireEvent.click(screen.getByRole('switch', { name: TOGGLE_DESC }))
    await waitFor(() =>
      expect(api.toggleBuiltinDeniedCommand).toHaveBeenCalledWith(
        'aws-destructive-cfn-delete-stack',
        true,
      ),
    )
    // No confirm modal for enabling.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('pinned rows render locked and never call the mutation', async () => {
    await renderPanel()

    const pinnedToggle = screen.getByRole('switch', { name: PINNED_DESC })
    // Pinned toggle is disabled (forced on, un-opt-out-able).
    expect(pinnedToggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(pinnedToggle)
    expect(api.toggleBuiltinDeniedCommand).not.toHaveBeenCalled()
    // No modal opens either.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('disable-all stays available and functional when governance-locked', async () => {
    // A policy pin on ONE rule sets governance_locked, but the backend keeps
    // pinned rules enforced under disable_all — so the disable-all control must
    // remain operable to opt every OTHER (unpinned) rule out.
    await renderPanel(snapshot({ governance_locked: true }))

    const disableAll = screen.getByRole('switch', { name: 'Disable all built-in denies' })
    expect(disableAll).toBeEnabled()
    expect(disableAll).toHaveAttribute('aria-checked', 'false')

    // Turning it ON opens the confirm modal (same guarded flow as unlocked).
    fireEvent.click(disableAll)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(
      screen.getByLabelText("I understand this weakens Kiro Crew's protection."),
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(api.setDeniedCommandsDisableAll).toHaveBeenCalledWith(true))
  })

  it('add-pattern validates the regex: invalid shows inline error, no API call', async () => {
    await renderPanel()

    const input = screen.getByLabelText('Custom deny pattern')
    fireEvent.change(input, { target: { value: '(unclosed' } })
    // Submit via Enter.
    fireEvent.keyDown(input, { key: 'Enter' })

    // Inline error surfaces and no API call is made.
    await screen.findByText(/Invalid regular expression|Unterminated group|Invalid group/i)
    expect(api.addUserDeniedCommand).not.toHaveBeenCalled()

    // A valid pattern clears the error and calls the API.
    fireEvent.change(input, { target: { value: 'rm -rf /data' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(api.addUserDeniedCommand).toHaveBeenCalledWith('rm -rf /data'),
    )
  })

  it('delete is only available on user rows', async () => {
    await renderPanel()

    // The delete affordance targets the user pattern specifically.
    const del = screen.getByLabelText(`Delete pattern ${USER_PATTERN}`)
    fireEvent.click(del)
    await waitFor(() =>
      expect(api.deleteUserDeniedCommand).toHaveBeenCalledWith('user-1'),
    )
    // Built-in rows have no delete affordance.
    expect(screen.queryByLabelText(`Delete pattern ${TOGGLE_DESC}`)).not.toBeInTheDocument()
  })

  it('chevron reveals the built-in pattern text', async () => {
    await renderPanel()

    // Pattern is hidden until the row is expanded.
    expect(screen.queryByText('aws.*cloudformation.*delete-stack.*')).not.toBeInTheDocument()
    const showButtons = screen.getAllByLabelText('Show pattern')
    fireEvent.click(showButtons[0])
    expect(screen.getByText('aws.*cloudformation.*delete-stack.*')).toBeInTheDocument()
  })
})

describe('SecurityPanel — governance policy viewer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
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
})

describe('SecurityPanel — posture disclosure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
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

describe('SecurityPanel — inspector rail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  /** Every rail row, in DOM order. */
  function railRows() {
    return screen.getAllByRole('option')
  }

  /** Echoes the ROUTER's query string. MemoryRouter never touches
   *  window.location, so asserting on that would silently pass forever. */
  function SearchProbe() {
    return <div data-testid="search">{useLocation().search}</div>
  }

  it('renders one row per section and selects the first when the URL names none', async () => {
    renderWithProviders(<SecurityPanel />)

    const rows = railRows()
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('Live Security Posture'),
      expect.stringContaining('YOLO (auto-approve)'),
      expect.stringContaining('Denied Commands'),
      expect.stringContaining('Tailnet origin'),
      expect.stringContaining('Third-party apps'),
      expect.stringContaining('Defense-in-Depth Architecture'),
      expect.stringContaining('Governance Policy'),
      expect.stringContaining('Documentation'),
    ])
    expect(rows[0]).toHaveAttribute('aria-selected', 'true')
    // ...and exactly one row is selected, so the rail never reads as two panes.
    expect(rows.filter(r => r.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })

  it('mounts ONLY the selected section', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })
    await screen.findByText('30 credential paths')

    // The rule table, the ceiling viewer and the docs links all belong to other
    // sections: keeping them unmounted is the point of the rail (the built-in
    // table is 137 rows) and is what makes each pane a screenful.
    expect(screen.queryByRole('button', { name: 'Expand all' })).not.toBeInTheDocument()
    expect(screen.queryByText('No enterprise policy in effect')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Security deep dive' })).not.toBeInTheDocument()
  })

  it('choosing a section swaps the pane and records it in the URL', async () => {
    renderWithProviders(<><SecurityPanel /><SearchProbe /></>, { route: '/?section=posture' })
    await screen.findByText('30 credential paths')

    fireEvent.click(screen.getByRole('option', { name: /Denied Commands/ }))

    // The rules pane is now mounted...
    expect(await screen.findByRole('button', { name: 'Expand all' })).toBeInTheDocument()
    // ...the posture pane is gone...
    expect(screen.queryByText('30 credential paths')).not.toBeInTheDocument()
    // ...and the choice is a deep link, so the section survives a reload and can
    // be targeted by a command-palette result.
    expect(screen.getByTestId('search')).toHaveTextContent('section=rules')
  })

  it('an unreadable third-party-apps value gets NO rail summary, rather than reading "Off"', async () => {
    // Same rule the card itself follows: a failed read is not "off". A rail badge
    // saying "Off" while third-party code is in fact admitted would be a false
    // reassurance in the most glanceable place on the page.
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('nope'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    const row = screen.getByRole('option', { name: /Third-party apps/ })
    await waitFor(() => {
      expect(row).toHaveTextContent('Third-party apps')
      expect(row).not.toHaveTextContent('Blocked')
      expect(row).not.toHaveTextContent('Allowed')
    })
  })

  it('summarises an active grant by WHEN IT ENDS, not by repeating the label', async () => {
    // The state that is currently weakening the install outranks the setting —
    // but echoing the section's own label made the rail's most important row
    // read "YOLO (auto-approve)" twice, stacked. The expiry is the useful fact.
    const store = createTestStore({
      dashboard: { status: { yolo: true, yolo_until_shutdown: true } } as never,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
    expect(row).toHaveTextContent('Until restart')
    // The label appears once, not twice.
    expect(row.textContent!.match(/YOLO \(auto-approve\)/g)).toHaveLength(1)
  })

  it('falls back to a bare active marker when no expiry is known', async () => {
    const store = createTestStore({ dashboard: { status: { yolo: true } } as never })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    expect(screen.getByRole('option', { name: /YOLO \(auto-approve\)/ }))
      .toHaveTextContent('Active now')
  })

  it('falls back to the active marker rather than "Until —" for an unparseable expiry', async () => {
    // The backend sends ISO-or-empty, so this guards the field's shape rather
    // than a live path: formatting an unparseable value yields an em-dash
    // placeholder, and "Until —" would assert a live grant while withholding
    // the expiry that makes the assertion actionable.
    const store = createTestStore({
      dashboard: { status: { yolo: true, yolo_expires_at: 'not-a-timestamp' } } as never,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
    expect(row).toHaveTextContent('Active now')
    expect(row).not.toHaveTextContent('—')
  })

  it('shows a same-day expiry as a bare clock time, without seconds', async () => {
    // The row is an 11px line that truncates, so "11:40:00 AM" spends three
    // characters on precision no reader acts on. Clock pinned so the assertion
    // does not depend on the day the suite happens to run.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-07T12:00:00Z'))
    try {
      const store = createTestStore({
        dashboard: {
          status: { yolo: true, yolo_expires_at: '2026-08-07T18:40:00Z' },
        } as never,
      })
      renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

      const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
      expect(row).toHaveTextContent('Until 6:40 PM')
      expect(row).not.toHaveTextContent('6:40:00')
      // The label still appears once, not twice.
      expect(row.textContent!.match(/YOLO \(auto-approve\)/g)).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('names the day when the expiry is not today, so it cannot read as already past', async () => {
    // The offered durations reach 24h. A bare "Until 8:00 AM" on a grant that
    // ends tomorrow morning reads as a time already gone by — on a security row
    // that means believing a live grant has expired.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-07T20:00:00Z'))
    try {
      const store = createTestStore({
        dashboard: {
          status: { yolo: true, yolo_expires_at: '2026-08-08T08:00:00Z' },
        } as never,
      })
      renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

      const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
      // Saturday 2026-08-08 — the weekday disambiguates it from today.
      expect(row).toHaveTextContent('Sat')
      expect(row).toHaveTextContent('8:00 AM')
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('SecurityPanel — rule search', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  const SEARCH = 'Search rules, patterns, categories…'

  async function renderRules() {
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
    return screen.findByLabelText(SEARCH)
  }

  it('matches on description and reveals the hit without expanding anything', async () => {
    const box = await renderRules()
    // Categories are collapsed on arrival, so the rule row is not in the DOM yet.
    expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    // A filter whose hits stay folded away is a filter that did nothing.
    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(PINNED_DESC)).not.toBeInTheDocument()
  })

  it('matches on the pattern text, not just the description', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'terminate-instances' } })

    expect(await screen.findByLabelText(PINNED_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument()
  })

  it('a category-name match keeps the whole category', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'aws destr' } })

    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.getByLabelText(PINNED_DESC)).toBeInTheDocument()
  })

  it('the category badge keeps the SHIPPED denominator while filtered', async () => {
    // The load-bearing assertion of this feature: a filter must never make the
    // gate read as smaller than it is. Showing "1/1" for a single hit inside a
    // 2-rule category would tell the reader a rule is not enforced.
    const box = await renderRules()
    expect(await screen.findByText('2/2')).toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.getByText('2/2')).toBeInTheDocument()
    expect(screen.queryByText('1/1')).not.toBeInTheDocument()
    // The match count is reported as a ratio against the shipped total instead.
    expect(screen.getByText(/1 \/ 2 rules/)).toBeInTheDocument()
  })

  it('says when the filter is what emptied the custom-pattern card', async () => {
    // Otherwise an empty Card B reads as "you have no custom patterns" while one
    // is configured and enforced.
    const box = await renderRules()
    expect(await screen.findByText(USER_PATTERN)).toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    expect(screen.queryByText(USER_PATTERN)).not.toBeInTheDocument()
    expect(
      screen.getByText('Your custom patterns are hidden by the current filter.'),
    ).toBeInTheDocument()
  })

  it('reports a miss instead of an empty list', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'zzzz' } })

    expect(await screen.findByText('No rules match “zzzz”.')).toBeInTheDocument()
  })

  it('clearing the filter restores the collapsed accordion state', async () => {
    // The filter force-opens its hits; clearing it must hand the accordion back
    // to whatever the user had, not leave every category expanded.
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'cloudformation' } })
    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()

    fireEvent.change(box, { target: { value: '' } })

    await waitFor(() => expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument())
  })
})

describe('SecurityPanel — review-round regressions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  const SEARCH = 'Search rules, patterns, categories…'

  it('keeps a half-typed deny pattern when the reader switches section and back', async () => {
    // The rules pane unmounts when another section is selected, so the draft is
    // held by the shell. Local state here would silently discard a pattern the
    // user was still typing — a regression against the old single-scroll panel,
    // where this input never unmounted.
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })

    const input = await screen.findByLabelText('Custom deny pattern')
    fireEvent.change(input, { target: { value: 'rm -rf /important' } })

    // Leave for another section...
    fireEvent.click(screen.getByRole('option', { name: /Live Security Posture/ }))
    expect(screen.queryByLabelText('Custom deny pattern')).not.toBeInTheDocument()

    // ...and come back.
    fireEvent.click(screen.getByRole('option', { name: /Denied Commands/ }))
    expect(await screen.findByLabelText('Custom deny pattern')).toHaveValue('rm -rf /important')
  })

  it('reports NO approval summary until the status payload lands', async () => {
    // `dashboard.status` is `StatusData | null` and starts as null, so an
    // `=== undefined` guard never fires. Claiming the reassuring "Interactive"
    // before the payload arrives would assert a security state we cannot know —
    // on an install where auto-approve may be active.
    const store = createTestStore({ dashboard: { status: null } as never })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
    expect(row).not.toHaveTextContent('Interactive')
  })

  it('drops the expand/collapse controls while filtering, instead of leaving them inert', async () => {
    // Matches render open regardless of the accordion state, so both controls
    // and the per-category chevron would record state the user cannot see apply.
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
    const box = await screen.findByLabelText(SEARCH)

    expect(screen.getByRole('button', { name: 'Expand all' })).toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Expand all' })).not.toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: 'Collapse all' })).not.toBeInTheDocument()
    // ...and the category header is no longer an expand/collapse button either.
    expect(screen.queryByRole('button', { name: /category rules/ })).not.toBeInTheDocument()
  })

  it('exposes the rail groups to assistive tech instead of hiding the headers', async () => {
    // The headers carry the yours-vs-enforced split that the rail exists to
    // convey; aria-hidden headers left screen-reader users with seven flat
    // options. listbox > group > option is the ARIA-valid way to keep it.
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    for (const name of ['Status', 'Your settings', 'Enforced', 'Reference']) {
      expect(screen.getByRole('group', { name })).toBeInTheDocument()
    }
    // The listbox keeps exactly one accessible name — naming the wrapper too
    // made a screen reader announce it twice.
    expect(screen.getAllByRole('listbox', { name: 'Security sections' })).toHaveLength(1)
  })
})
