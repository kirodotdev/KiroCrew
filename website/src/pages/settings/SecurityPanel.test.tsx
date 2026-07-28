import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { DeniedCommandsData } from '../../api/client'

/* ── api client mock ───────────────────────────────────────────────────────
 * SecurityPanel drives all its mutations through the `api` client methods.  We
 * mock those methods so no network I/O happens; each returns the (mutated)
 * snapshot the panel then re-renders from.  `securityStats` is unrelated to the
 * denied-commands surface but is queried by the status card, so it is stubbed.
 */
vi.mock('../../api/client', () => ({
  api: {
    securityStats: vi.fn().mockResolvedValue({
      denied_commands: 2,
      suspicious_patterns: 42,
      tool_schemas: 12,
      redaction_paths: 5,
    }),
    deniedCommands: vi.fn(),
    toggleBuiltinDeniedCommand: vi.fn(),
    setDeniedCommandsDisableAll: vi.fn(),
    addUserDeniedCommand: vi.fn(),
    toggleUserDeniedCommand: vi.fn(),
    deleteUserDeniedCommand: vi.fn(),
    governancePolicy: vi.fn(),
  },
}))

import { api } from '../../api/client'
import type { GovernancePolicyData } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const PINNED_DESC = 'Blocks EC2 instance termination'
const TOGGLE_DESC = 'Blocks CloudFormation stack deletion'
const USER_PATTERN = 'rm -rf /tmp/mine'

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
  const utils = renderWithProviders(<SecurityPanel />)
  // Wait for the async query to hydrate the category accordion, then expand all.
  const expandAll = await screen.findByRole('button', { name: 'Expand all' })
  fireEvent.click(expandAll)
  await screen.findByLabelText(TOGGLE_DESC)
  return utils
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

describe('SecurityPanel — denied commands', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.toggleBuiltinDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.setDeniedCommandsDisableAll as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.addUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.toggleUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.deleteUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
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
      screen.getByLabelText("I understand this weakens KiroCrew's protection."),
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
    // remain operable to opt every OTHER (unpinned) rule out. Regression for
    // the bug where the toggle was removed entirely when locked.
    await renderPanel(snapshot({ governance_locked: true }))

    const disableAll = screen.getByRole('switch', { name: 'Disable all built-in denies' })
    expect(disableAll).toBeEnabled()
    expect(disableAll).toHaveAttribute('aria-checked', 'false')

    // Turning it ON opens the confirm modal (same guarded flow as unlocked).
    fireEvent.click(disableAll)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(
      screen.getByLabelText("I understand this weakens KiroCrew's protection."),
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

  it('status row shows the effective_count', async () => {
    await renderPanel(snapshot({ effective_count: 129 }))
    expect(await screen.findByText('129 active')).toBeInTheDocument()
  })

  it('status rows reserve the external-link slot so every badge shares one right edge', async () => {
    // Regression: the hover-only ExternalLink used to render ONLY on rows with
    // an href, pushing those badges left of the unlinked rows' badges.
    await renderPanel()

    // 'Standard' (Process Sandbox) is linked; 'Interactive' (Tool Approval) is not.
    for (const text of ['Standard', 'Interactive']) {
      const trailing = screen.getByText(text).parentElement
      expect(trailing?.children).toHaveLength(2)
    }
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
  })

  it('shows the standalone "no enterprise policy" state when has_policy is false', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    renderWithProviders(<SecurityPanel />)

    expect(await screen.findByText('No enterprise policy in effect')).toBeInTheDocument()
    expect(
      screen.getByText(/No policy or host profile restricts the host surface \(standalone mode\)/),
    ).toBeInTheDocument()
    // No governed rows are rendered in standalone mode.
    expect(screen.queryByText('policy ∩ profile')).not.toBeInTheDocument()
  })

  it('renders governed + ungoverned rows with effective state and source', async () => {
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govGoverned())
    renderWithProviders(<SecurityPanel />)

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
    renderWithProviders(<SecurityPanel />)

    expect(await screen.findByText(/Governance status is temporarily unavailable/)).toBeInTheDocument()
  })
})
