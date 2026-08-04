import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { DeniedCommandsData } from '../../api/client'

/* ── api client mock ───────────────────────────────────────────────────────
 * SecurityPanel drives all its mutations through the `api` client methods.  We
 * mock those methods so no network I/O happens; each returns the (mutated)
 * snapshot the panel then re-renders from.  `securityPosture` feeds the Live
 * Security Posture card, and `governancePolicy` feeds the ceiling viewer.
 */
vi.mock('../../api/client', () => ({
  api: {
    deniedCommands: vi.fn(),
    toggleBuiltinDeniedCommand: vi.fn(),
    setDeniedCommandsDisableAll: vi.fn(),
    addUserDeniedCommand: vi.fn(),
    toggleUserDeniedCommand: vi.fn(),
    deleteUserDeniedCommand: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
  },
}))

import { api } from '../../api/client'
import type { GovernancePolicyData, SecurityPostureData } from '../../api/client'
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
    // remain operable to opt every OTHER (unpinned) rule out.
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

  it('the denied-commands posture pill shows enabled built-ins, not the shipped rule total', async () => {
    // The posture registry reports the SHIPPED built-in table (137); the pill must
    // show what is actually enforced after opt-outs + policy pins. Counted from
    // `dc.builtins` (1 of the 2 fixture rules disabled → 1), NOT `effective_count`,
    // which also includes user_added and would overshoot the denominator.
    await renderPanel(
      snapshot({
        builtins: snapshot().builtins.map((b, i) => (i === 0 ? { ...b, enabled: false } : b)),
        effective_count: 129,
      }),
    )
    expect(await screen.findByText('1 built-in rules')).toBeInTheDocument()
    expect(screen.queryByText('137 built-in rules')).not.toBeInTheDocument()
    expect(screen.queryByText('129 built-in rules')).not.toBeInTheDocument()
  })

  it('status rows reserve the external-link slot so every badge shares one right edge', async () => {
    // The hover-only ExternalLink slot is reserved on every row, linked or not,
    // so all badges share one right edge; rendering it only on rows with an
    // href would push those badges left of the unlinked rows' badges.
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

describe('SecurityPanel — posture disclosure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
  })

  it('renders a pill per control using the server-derived count and unit', async () => {
    renderWithProviders(<SecurityPanel />)

    // The whole point of the change: the count is data, not a hardcoded literal.
    expect(await screen.findByText('2 output paths')).toBeInTheDocument()
    expect(screen.getByText('30 credential paths')).toBeInTheDocument()
  })

  it('items are hidden until the row is expanded, then reveal label + detail', async () => {
    renderWithProviders(<SecurityPanel />)
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
    renderWithProviders(<SecurityPanel />)
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
    renderWithProviders(<SecurityPanel />)
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
    renderWithProviders(<SecurityPanel />)

    expect(await screen.findByText('unavailable')).toBeInTheDocument()
    expect(screen.queryByText('0 output paths')).not.toBeInTheDocument()
  })

  it('shows a soft notice when the posture endpoint fails', async () => {
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />)

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
    renderWithProviders(<SecurityPanel />)

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
    renderWithProviders(<SecurityPanel />)

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
    renderWithProviders(<SecurityPanel />)

    expect(await screen.findByText('2 built-in rules')).toBeInTheDocument()
    expect(screen.queryByText('3 built-in rules')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(/^Show Denied commands details/))
    expect(
      await screen.findByText(/2 of 2 built-in rules are currently enforced/),
    ).toBeInTheDocument()
    // Custom patterns are accounted for, just not folded into the built-in ratio.
    expect(screen.getByText(/1 custom pattern are counted separately below/)).toBeInTheDocument()
  })

  it('clearing a filter re-applies the truncation cap', async () => {
    // `expanded` must reset on a filter change: otherwise expanding a FILTERED
    // subset and then clearing the filter renders the whole list, defeating the
    // INITIAL_VISIBLE DOM cap. Must expand *while filtered* to exercise it.
    renderWithProviders(<SecurityPanel />)
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
    renderWithProviders(<SecurityPanel />)

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
    renderWithProviders(<SecurityPanel />)

    expect(await screen.findByText('Brand new control')).toBeInTheDocument()
    expect(screen.getByText('3 widgets')).toBeInTheDocument()
  })
})
