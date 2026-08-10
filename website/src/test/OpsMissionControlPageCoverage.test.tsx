/**
 * Coverage for the Ops Mission Control board page.
 *
 * The page is a builtin dashboard route that talks to its backend through
 * `opsApi` (plain same-origin fetch, not the app-sdk hooks), so the seam under
 * test is that module: every route is stubbed and the real helpers
 * (`describeVerification`, `entryIsProven`, `blockedLabel`, …) are kept, since
 * the page's branches are decided by what those helpers return.
 *
 * The three sibling tabs and the embedded chat are replaced with markers. They
 * own their own queries — `IncidentChat` mounts the dashboard's real chat embed
 * behind an `AppApiProvider` — and mounting them here would test them rather
 * than the board.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import {
  SIGNALS_QUERY_KEY,
  opsApi,
  type BoardState,
  type Incident,
  type IncidentStatus,
  type LedgerEntry,
  type LedgerStats,
  type OperatingMode,
  type PendingProposal,
  type ProviderInfo,
  type RotationInfo,
  type Signal,
  type SignalsResult,
} from '../apps/ops-mission-control/api'
import OpsMissionControlPage from '../apps/ops-mission-control/OpsMissionControlPage'

vi.mock('../apps/ops-mission-control/api', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../apps/ops-mission-control/api')>()
  return {
    ...actual,
    opsApi: {
      state: vi.fn(),
      incidents: vi.fn(),
      incident: vi.fn(),
      transition: vi.fn(),
      dispatch: vi.fn(),
      ledger: vi.fn(),
      decideProposal: vi.fn(),
      signals: vi.fn(),
    },
  }
})

vi.mock('../apps/ops-mission-control/SettingsPanel', () => ({
  default: () => <div data-testid="stub-settings" />,
}))
vi.mock('../apps/ops-mission-control/SignalsPanel', () => ({
  default: () => <div data-testid="stub-signals" />,
}))
vi.mock('../apps/ops-mission-control/HandoverPanel', () => ({
  default: () => <div data-testid="stub-handover" />,
}))
vi.mock('../apps/ops-mission-control/IncidentChat', () => ({
  default: ({ incidentId }: { incidentId: string }) => (
    <div data-testid="stub-chat" data-incident={incidentId} />
  ),
}))

const agoIso = (secs: number) => new Date(Date.now() - secs * 1000).toISOString()

const mkSignal = (over: Partial<Signal> = {}): Signal => ({
  id: 'sig-1',
  source: 'cloudwatch',
  title: 'Checkout latency above threshold',
  severity: 'critical',
  state: 'firing',
  fired_at: agoIso(600),
  resource: 'svc/checkout',
  url: 'https://console.example.com/alarm/1',
  labels: {},
  fingerprint: 'fp-1',
  provider_key: '',
  suppressed_by: '',
  suppressed_reason: '',
  ...over,
})

const mkIncident = (over: Partial<Incident> = {}): Incident => ({
  incident_id: 'omc-0001',
  signal: mkSignal(),
  status: 'investigating',
  operating_mode: 'propose',
  claimed_at: agoIso(600),
  updated_at: agoIso(90),
  slot_key: 'ops-mission-control-omc-0001',
  slack_thread_ts: '',
  ledger_matches: [],
  diagnosis: '',
  proposed_action: null,
  resolution: '',
  ...over,
})

const mkEntry = (over: Partial<LedgerEntry> = {}): LedgerEntry => ({
  entry_id: 'entry-aaaabbbb',
  pattern: 'Latency spike on a cold cache',
  fix: 'Warm the cache and re-check',
  fingerprints: ['fp-1'],
  provider_keys: [],
  confidence: 'high',
  trust: 'verified',
  use_count: 4,
  miss_count: 0,
  last_miss: '',
  first_seen: agoIso(90000),
  last_used: agoIso(400),
  source: 'cloudwatch',
  ...over,
})

const mkProvider = (over: Partial<ProviderInfo> = {}): ProviderInfo => ({
  id: 'cloudwatch',
  display_name: 'CloudWatch',
  roles: ['signal'],
  configured: true,
  config_fields: [],
  secret_fields: [],
  detail: '',
  config: {},
  secrets: {},
  ...over,
})

const mkRotation = (over: Partial<RotationInfo> = {}): RotationInfo => ({
  on_shift: true,
  who: '',
  until: '',
  unknown: false,
  tiers: {},
  armed_crons: [],
  tier_crons: {},
  mode: 'observe',
  rules: 0,
  primary: true,
  modes_available: ['observe', 'propose', 'act'],
  ...over,
})

const mkStats = (over: Partial<LedgerStats> = {}): LedgerStats => ({
  total: 7,
  verified: 3,
  high_confidence: 3,
  total_uses: 12,
  proven: 2,
  demoted: 0,
  total_misses: 0,
  ...over,
})

const mkState = (over: Partial<BoardState> = {}): BoardState => ({
  incidents: [],
  counts: {},
  providers: [mkProvider()],
  rotation: mkRotation(),
  ledger: mkStats(),
  webhook_queue: 0,
  ...over,
})

const mkSignalsResult = (over: Partial<SignalsResult> = {}): SignalsResult => ({
  signals: [],
  firing: [],
  cleared: [],
  suppressed: [],
  unclaimed: [],
  errors: {},
  poll_health: { cloudwatch: { ok: true, detail: '', at: agoIso(30), signals: 0 } },
  all_sources_healthy: true,
  ...over,
})

const mkProposal = (over: Partial<PendingProposal> = {}): PendingProposal => ({
  state: 'pending',
  action: 'silence',
  sink: 'cloudwatch',
  note: 'Silencing while the deploy rolls back.',
  duration_secs: 900,
  digest: 'digest-abc',
  proposed_at: agoIso(120),
  expires_at: agoIso(-3600),
  decided_at: '',
  ...over,
})

/** Default happy-path stubs; individual tests override the route they exercise. */
function stubRoutes(state: BoardState = mkState(), entries: LedgerEntry[] = []) {
  vi.mocked(opsApi.state).mockResolvedValue(state)
  vi.mocked(opsApi.ledger).mockResolvedValue({ entries, stats: state.ledger })
  vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [] })
}

/** Open the expanded detail panel of the first incident row. */
async function expandFirstRow() {
  const row = await waitFor(() => screen.getAllByTestId('omc-incident-row')[0])
  fireEvent.click(row)
  await waitFor(() => expect(screen.getByTestId('stub-chat')).toBeInTheDocument())
}

/**
 * Pick a tab. jsdom reports zero width for every element, so `SegmentedControl`
 * measures its parent as too narrow and collapses to its dropdown form: one
 * trigger button carrying the ACTIVE label, and the options only in the open
 * popup. The trigger precedes the popup in the DOM, hence the last-match pick —
 * when the target is already active, both exist and only the popup one selects.
 */
async function switchTab(label: 'Board' | 'Signals' | 'Handover' | 'Settings') {
  const trigger = screen.getAllByRole('button', {
    name: /^(Board|Signals|Handover|Settings)$/,
  })[0]
  fireEvent.click(trigger)
  const options = await waitFor(() => screen.getAllByRole('button', { name: label }))
  fireEvent.click(options[options.length - 1])
}

describe('OpsMissionControlPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a loading board while /state is in flight', async () => {
    vi.mocked(opsApi.state).mockReturnValue(new Promise<BoardState>(() => {}))
    vi.mocked(opsApi.ledger).mockReturnValue(
      new Promise<{ entries: LedgerEntry[]; stats: LedgerStats }>(() => {}),
    )
    vi.mocked(opsApi.incidents).mockReturnValue(
      new Promise<{ incidents: Incident[] }>(() => {}),
    )
    renderWithProviders(<OpsMissionControlPage />)
    expect(screen.getByText('Mission Control')).toBeInTheDocument()
    // Both the board and the closed-postmortems card are waiting.
    await waitFor(() => expect(screen.getAllByText('Loading…')).toHaveLength(2))
  })

  it('reports an unverified quiet board when no poll has been made', async () => {
    stubRoutes()
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByText('No incidents claimed')).toBeInTheDocument(),
    )
    expect(
      screen.getByText(/Source health has not been verified this session/),
    ).toBeInTheDocument()
  })

  it('tells a brand-new install to connect a provider first', async () => {
    stubRoutes(mkState({ providers: [] }))
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(
        screen.getByText('Connect a provider in Settings to start watching.'),
      ).toBeInTheDocument(),
    )
  })

  it('claims the quiet is real only when the cached poll says every source answered', async () => {
    stubRoutes()
    const { queryClient } = renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByText('No incidents claimed')).toBeInTheDocument(),
    )
    act(() => {
      queryClient.setQueryData(SIGNALS_QUERY_KEY, mkSignalsResult())
    })
    await waitFor(() => expect(screen.getByText('Nothing is firing')).toBeInTheDocument())
    expect(
      screen.getByText('Every configured source answered the last poll, so this quiet is real.'),
    ).toBeInTheDocument()
  })

  it('names the sources that did not answer instead of calling the board quiet', async () => {
    stubRoutes()
    const { queryClient } = renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByText('No incidents claimed')).toBeInTheDocument(),
    )
    act(() => {
      queryClient.setQueryData(
        SIGNALS_QUERY_KEY,
        mkSignalsResult({
          all_sources_healthy: false,
          errors: { cloudwatch: 'expired credentials' },
          poll_health: { cloudwatch: { ok: false, detail: 'expired credentials', at: agoIso(30) } },
        }),
      )
    })
    await waitFor(() =>
      expect(
        screen.getByText('Nothing claimed — but the board is not verified'),
      ).toBeInTheDocument(),
    )
    expect(
      screen.getByText(
        'CloudWatch did not answer the last poll, so absence of a signal does not mean recovery.',
      ),
    ).toBeInTheDocument()
  })

  it('falls back to an unnamed source when the failing one is not a signal provider', async () => {
    stubRoutes(mkState({ providers: [mkProvider({ roles: ['action'] })] }))
    const { queryClient } = renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByText('No incidents claimed')).toBeInTheDocument(),
    )
    act(() => {
      queryClient.setQueryData(
        SIGNALS_QUERY_KEY,
        mkSignalsResult({ all_sources_healthy: false }),
      )
    })
    await waitFor(() =>
      expect(
        screen.getByText(
          'At least one source did not answer the last poll, so absence of a signal does not mean recovery.',
        ),
      ).toBeInTheDocument(),
    )
  })

  it('renders the five stat cards from the board and the ledger rollup', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident(),
          mkIncident({
            incident_id: 'omc-0002',
            status: 'needs_human',
            blocked_reason: 'awaiting_approval',
            signal: mkSignal({ id: 'sig-2', title: 'Queue backing up', severity: 'warning' }),
          }),
          mkIncident({ incident_id: 'omc-0003', status: 'resolved' }),
        ],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getAllByTestId('omc-incident-row')).toHaveLength(3),
    )
    const cardValue = (label: string) => {
      const el = screen.getByText(label).closest('[data-testid="stat-card"]')
      return el?.querySelector('[data-testid="stat-card-value"]')?.textContent
    }
    expect(cardValue('Active')).toBe('2')
    expect(cardValue('Waiting on you')).toBe('1')
    expect(cardValue('Sources wired')).toBe('1')
    expect(cardValue('Patterns known')).toBe('7')
    expect(cardValue('Patterns proven')).toBe('2')
    // The blocked reason replaces the bare status in the row.
    expect(screen.getByText('Approve to continue')).toBeInTheDocument()
    expect(screen.getByText('Investigating')).toBeInTheDocument()
    expect(screen.getByText('Resolved')).toBeInTheDocument()
  })

  it('renders a compact age per row and an em dash for an unparseable timestamp', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident({ incident_id: 'omc-secs', updated_at: agoIso(20) }),
          mkIncident({ incident_id: 'omc-mins', updated_at: agoIso(90) }),
          mkIncident({ incident_id: 'omc-hours', updated_at: agoIso(7200) }),
          mkIncident({ incident_id: 'omc-days', updated_at: agoIso(200000) }),
          mkIncident({ incident_id: 'omc-bad', updated_at: 'not-a-date', claimed_at: '' }),
        ],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getAllByTestId('omc-incident-row')).toHaveLength(5),
    )
    expect(screen.getByText('20s')).toBeInTheDocument()
    expect(screen.getByText('1m')).toBeInTheDocument()
    expect(screen.getByText('2h')).toBeInTheDocument()
    expect(screen.getByText('2d')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('expands a row into its detail panel and mounts the investigation chat', async () => {
    stubRoutes(
      mkState({
        incidents: [mkIncident({ diagnosis: 'The cache was cold after the deploy.' })],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('Source')).toBeInTheDocument()
    expect(screen.getByText('svc/checkout')).toBeInTheDocument()
    expect(screen.getByText('Known patterns')).toBeInTheDocument()
    expect(screen.getByText('none matched')).toBeInTheDocument()
    expect(screen.getByText('The cache was cold after the deploy.')).toBeInTheDocument()
    // The incident's own operating mode, distinct from the header's rotation badge.
    expect(screen.getByText('Propose')).toBeInTheDocument()
    expect(screen.getByTestId('stub-chat')).toHaveAttribute('data-incident', 'omc-0001')
    // Clicking the same row again collapses it.
    fireEvent.click(screen.getAllByTestId('omc-incident-row')[0])
    await waitFor(() => expect(screen.queryByTestId('stub-chat')).toBeNull())
  })

  it('warns that a ledger match is only a shape match when the provider publishes no identity', async () => {
    stubRoutes(mkState({ incidents: [mkIncident()] }))
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('Match basis')).toBeInTheDocument()
    expect(
      screen.getByText(/This provider published no exact identity/),
    ).toBeInTheDocument()
    expect(screen.getByText('No diagnosis recorded yet.')).toBeInTheDocument()
  })

  it('reports an exact match basis and truncates the provider key', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident({
            signal: mkSignal({ provider_key: 'k'.repeat(40) }),
          }),
        ],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(
      screen.getByText(`cloudwatch publishes an exact identity for this failure (${'k'.repeat(24)}), which the ledger prefers over our shape hash.`),
    ).toBeInTheDocument()
  })

  it('renders a matched ledger entry with its fix, counts and fast-path verdict', async () => {
    stubRoutes(
      mkState({ incidents: [mkIncident({ ledger_matches: ['entry-aaaabbbb'] })] }),
      [mkEntry()],
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('1 matched')).toBeInTheDocument()
    expect(screen.getByText('Fix: Warm the cache and re-check')).toBeInTheDocument()
    expect(screen.getByText('proven — agent may propose directly')).toBeInTheDocument()
    // The pattern renders in the match card AND in the ledger table below.
    expect(screen.getAllByText('Latency spike on a cold cache')).toHaveLength(2)
  })

  it('shows the miss count and the demotion note for a refuted entry', async () => {
    stubRoutes(
      mkState({ incidents: [mkIncident({ ledger_matches: ['entry-aaaabbbb'] })] }),
      [mkEntry({ miss_count: 2, last_miss: '2026-08-01' })],
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText(/failed 2×/)).toBeInTheDocument()
    expect(screen.getByText(/Demoted: this fix was applied/)).toBeInTheDocument()
    expect(screen.getByText(/most recently 2026-08-01/)).toBeInTheDocument()
    expect(screen.getByText('hypothesis — agent must confirm first')).toBeInTheDocument()
    // The table's own column marks the same entry demoted rather than locked.
    expect(screen.getByText('demoted')).toBeInTheDocument()
  })

  it('explains a locked entry whose only shortfall is the use floor', async () => {
    stubRoutes(
      mkState({ incidents: [mkIncident({ ledger_matches: ['entry-aaaabbbb'] })] }),
      [mkEntry({ use_count: 1 })],
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(
      screen.getByText(/Marked verified and high confidence, but used only/),
    ).toBeInTheDocument()
    expect(screen.getByText(/the fast path also needs/)).toBeInTheDocument()
  })

  it('names a matched entry that is no longer in the ledger rather than rendering nothing', async () => {
    stubRoutes(mkState({ incidents: [mkIncident({ ledger_matches: ['pruned-entry-id'] })] }))
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText(/Matched entry/)).toBeInTheDocument()
    expect(screen.getByText(/pruned-e/)).toBeInTheDocument()
    expect(screen.getByText(/is no longer in the/)).toBeInTheDocument()
  })

  it('renders the post-action verification verdict with the backend detail verbatim', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident({
            last_action: 'silence',
            last_action_at: agoIso(300),
            verification: 'still_firing',
            verification_detail: 'cloudwatch reported the alarm in ALARM at 12:04.',
          }),
        ],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('still firing after this action')).toBeInTheDocument()
    expect(screen.getByText(/sent 5m ago/)).toBeInTheDocument()
    expect(
      screen.getByText('cloudwatch reported the alarm in ALARM at 12:04.'),
    ).toBeInTheDocument()
  })

  it('says an action was sent without a timestamp when none was recorded', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident({ last_action: 'ack', last_action_at: '', verification: 'not_checkable' }),
        ],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('sent, not confirmed')).toBeInTheDocument()
    expect(screen.getByText(/^ack sent —$/)).toBeInTheDocument()
  })

  it('renders a pending verification with the muted clock branch', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident({
            last_action: 'comment',
            last_action_at: agoIso(60),
            verification: 'pending',
            verify_after: '2026-08-10T21:00:00Z',
          }),
        ],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('not checked yet')).toBeInTheDocument()
  })

  it('links out through the URL guard and drops a non-http scheme', async () => {
    stubRoutes(mkState({ incidents: [mkIncident()] }))
    const { unmount } = renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    const link = screen.getByText('Open in provider').closest('a')
    expect(link).toHaveAttribute('href', 'https://console.example.com/alarm/1')
    unmount()

    stubRoutes(
      mkState({
        incidents: [mkIncident({ signal: mkSignal({ url: 'javascript:alert(1)' }) })],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.queryByText('Open in provider')).toBeNull()
  })

  it('transitions an incident to resolved and reports that Slack replies land', async () => {
    const incident = mkIncident()
    stubRoutes(mkState({ incidents: [incident] }))
    vi.mocked(opsApi.transition).mockResolvedValue({
      incident,
      slack_thread_replyable: true,
    })
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    fireEvent.click(screen.getByRole('button', { name: 'Mark resolved' }))
    await waitFor(() =>
      expect(opsApi.transition).toHaveBeenCalledWith('omc-0001', 'resolved'),
    )
    await waitFor(() =>
      expect(
        screen.getByText('Replies in the Slack thread reach this investigation.'),
      ).toBeInTheDocument(),
    )
  })

  it('warns when a Slack reply would not reach the investigation', async () => {
    const incident = mkIncident({ status: 'needs_human' })
    stubRoutes(mkState({ incidents: [incident] }))
    vi.mocked(opsApi.transition).mockResolvedValue({
      incident,
      slack_thread_replyable: false,
    })
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    fireEvent.click(screen.getByRole('button', { name: 'Mark resolved' }))
    await waitFor(() =>
      expect(
        screen.getByText(/Replies in the Slack thread will NOT reach this investigation/),
      ).toBeInTheDocument(),
    )
  })

  it('surfaces a failed transition on the row that triggered it', async () => {
    stubRoutes(mkState({ incidents: [mkIncident()] }))
    vi.mocked(opsApi.transition).mockRejectedValue(new Error('no rule grants resolve'))
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    fireEvent.click(screen.getByRole('button', { name: 'Mark resolved' }))
    await waitFor(() =>
      expect(screen.getByText('no rule grants resolve')).toBeInTheDocument(),
    )
  })

  it('offers no resolve button for a status that cannot be resolved from the board', async () => {
    stubRoutes(mkState({ incidents: [mkIncident({ status: 'dispatched' })] }))
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.queryByRole('button', { name: 'Mark resolved' })).toBeNull()
  })

  it('renders a pending proposal with its stored terms and approves with the shown digest', async () => {
    stubRoutes(mkState({ incidents: [mkIncident({ proposed_action: mkProposal() })] }))
    vi.mocked(opsApi.decideProposal).mockResolvedValue({
      ok: true,
      proposal: mkProposal({ state: 'approved' }),
      executed: true,
    })
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('Awaiting your approval')).toBeInTheDocument()
    expect(screen.getByText('through cloudwatch for 900s')).toBeInTheDocument()
    expect(
      screen.getByText('Silencing while the deploy rolls back.'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Approve and run' }))
    await waitFor(() =>
      expect(opsApi.decideProposal).toHaveBeenCalledWith('omc-0001', true, 'digest-abc'),
    )
  })

  it('rejects a proposal without echoing a digest', async () => {
    stubRoutes(
      mkState({
        incidents: [
          mkIncident({ proposed_action: mkProposal({ action: 'ack', duration_secs: null }) }),
        ],
      }),
    )
    vi.mocked(opsApi.decideProposal).mockResolvedValue({
      ok: true,
      proposal: mkProposal({ state: 'rejected' }),
      executed: false,
    })
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.getByText('through cloudwatch')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    await waitFor(() =>
      expect(opsApi.decideProposal).toHaveBeenCalledWith('omc-0001', false, undefined),
    )
  })

  it('shows the gate refusal when a decision comes back not ok', async () => {
    stubRoutes(mkState({ incidents: [mkIncident({ proposed_action: mkProposal() })] }))
    vi.mocked(opsApi.decideProposal).mockResolvedValue({
      ok: false,
      proposal: null,
      executed: false,
      error: 'the draft moved since you read it',
    })
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    fireEvent.click(screen.getByRole('button', { name: 'Approve and run' }))
    await waitFor(() =>
      expect(screen.getByText('the draft moved since you read it')).toBeInTheDocument(),
    )
  })

  it('shows a refusal with no reason as the generic decision message', async () => {
    stubRoutes(mkState({ incidents: [mkIncident({ proposed_action: mkProposal() })] }))
    vi.mocked(opsApi.decideProposal).mockResolvedValue({
      ok: false,
      proposal: null,
      executed: false,
    })
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    fireEvent.click(screen.getByRole('button', { name: 'Approve and run' }))
    await waitFor(() =>
      expect(screen.getByText('The decision was refused.')).toBeInTheDocument(),
    )
  })

  it('surfaces a decision error from the autonomy gate', async () => {
    stubRoutes(mkState({ incidents: [mkIncident({ proposed_action: mkProposal() })] }))
    vi.mocked(opsApi.decideProposal).mockRejectedValue(new Error('403 no rule grants ack'))
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    fireEvent.click(screen.getByRole('button', { name: 'Approve and run' }))
    await waitFor(() =>
      expect(screen.getByText('403 no rule grants ack')).toBeInTheDocument(),
    )
  })

  it('hides a proposal that is no longer pending', async () => {
    stubRoutes(
      mkState({
        incidents: [mkIncident({ proposed_action: mkProposal({ state: 'expired' }) })],
      }),
    )
    renderWithProviders(<OpsMissionControlPage />)
    await expandFirstRow()
    expect(screen.queryByText('Awaiting your approval')).toBeNull()
  })

  it('reports a dispatch cycle that changed nothing, counting parked signals', async () => {
    stubRoutes()
    vi.mocked(opsApi.dispatch).mockResolvedValue({
      claimed: [],
      released: [],
      polled: 3,
      unclaimed_remaining: 0,
      errors: {},
      changed: false,
      skipped_reason: '',
      briefs: {},
      suppressed: 2,
      verifications: {},
    })
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(
        screen.getByText(
          'Polled 3 firing signals, plus 2 parked at the provider and left alone; nothing new to claim.',
        ),
      ).toBeInTheDocument(),
    )
  })

  it('reports a plain polled summary when nothing was parked', async () => {
    stubRoutes()
    vi.mocked(opsApi.dispatch).mockResolvedValue({
      claimed: [],
      released: [],
      polled: 1,
      unclaimed_remaining: 0,
      errors: {},
      changed: false,
      skipped_reason: '',
      briefs: {},
      suppressed: 0,
      verifications: {},
    })
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(
        screen.getByText('Polled 1 firing signal; nothing new to claim.'),
      ).toBeInTheDocument(),
    )
  })

  it('tells an unconfigured install why a cycle did nothing', async () => {
    stubRoutes(mkState({ providers: [] }))
    vi.mocked(opsApi.dispatch).mockResolvedValue({
      claimed: [],
      released: [],
      polled: 0,
      unclaimed_remaining: 0,
      errors: {},
      changed: false,
      skipped_reason: '',
      briefs: {},
      suppressed: 0,
      verifications: {},
    })
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(
        screen.getByText('No providers are set up yet — open Settings to connect one.'),
      ).toBeInTheDocument(),
    )
  })

  it('prefers the backend skip reason over its own wording', async () => {
    stubRoutes()
    vi.mocked(opsApi.dispatch).mockResolvedValue({
      claimed: [],
      released: [],
      polled: 0,
      unclaimed_remaining: 0,
      errors: {},
      changed: false,
      skipped_reason: 'off shift — a teammate holds the pager',
      briefs: {},
      suppressed: 0,
      verifications: {},
    })
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(
        screen.getByText('off shift — a teammate holds the pager'),
      ).toBeInTheDocument(),
    )
  })

  it('retracts its own claim when a verification came back still firing', async () => {
    stubRoutes()
    vi.mocked(opsApi.dispatch).mockResolvedValue({
      claimed: [],
      released: [],
      polled: 1,
      unclaimed_remaining: 0,
      errors: {},
      changed: true,
      skipped_reason: '',
      briefs: {},
      suppressed: 0,
      verifications: { 'omc-0001': 'still_firing', 'omc-0009': 'cleared' },
    })
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(
        screen.getByText(
          /omc-0001 was still firing when we re-read it after the action this app reported as applied\./,
        ),
      ).toBeInTheDocument(),
    )
  })

  it('summarises several failed verifications by id', async () => {
    stubRoutes()
    vi.mocked(opsApi.dispatch).mockResolvedValue({
      claimed: [],
      released: [],
      polled: 2,
      unclaimed_remaining: 0,
      errors: {},
      changed: true,
      skipped_reason: '',
      briefs: {},
      suppressed: 0,
      verifications: { 'omc-0001': 'still_firing', 'omc-0002': 'still_firing' },
    })
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(
        screen.getByText(/2 incidents were still firing .*omc-0001, omc-0002/),
      ).toBeInTheDocument(),
    )
  })

  it('surfaces a dispatch failure', async () => {
    stubRoutes()
    vi.mocked(opsApi.dispatch).mockRejectedValue(new Error('provider timed out'))
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Poll & claim' }))
    await waitFor(() =>
      expect(screen.getByText('provider timed out')).toBeInTheDocument(),
    )
  })

  it('switches to each sibling tab and hides the board action button', async () => {
    stubRoutes()
    renderWithProviders(<OpsMissionControlPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Board' })).toBeInTheDocument(),
    )
    await switchTab('Signals')
    await waitFor(() => expect(screen.getByTestId('stub-signals')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Poll & claim' })).toBeNull()

    await switchTab('Handover')
    await waitFor(() => expect(screen.getByTestId('stub-handover')).toBeInTheDocument())

    await switchTab('Settings')
    await waitFor(() => expect(screen.getByTestId('stub-settings')).toBeInTheDocument())

    await switchTab('Board')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Poll & claim' })).toBeInTheDocument(),
    )
  })

  describe('shift and mode badges', () => {
    const cases: [string, Partial<RotationInfo>, string][] = [
      ['on shift with no name', { on_shift: true }, 'On shift'],
      [
        'on shift with a name and a parsed shift end',
        { on_shift: true, who: 'octocat', until: '2026-08-11T02:00:00Z' },
        'On shift: octocat until',
      ],
      [
        'off shift naming who holds the pager',
        { on_shift: false, who: 'octocat' },
        'off shift — octocat is on call',
      ],
      ['off shift with nobody named', { on_shift: false }, 'off shift'],
      [
        'indeterminate but still armed',
        { on_shift: true, unknown: true },
        'rotation unknown — tier armed',
      ],
      [
        'indeterminate and disarmed',
        { on_shift: false, unknown: true },
        'rotation unknown — not picking up work',
      ],
    ]

    for (const [name, rotation, expected] of cases) {
      it(`labels the header badge: ${name}`, async () => {
        stubRoutes(mkState({ rotation: mkRotation(rotation) }))
        renderWithProviders(<OpsMissionControlPage />)
        await waitFor(() =>
          expect(screen.getByText(new RegExp(expected))).toBeInTheDocument(),
        )
      })
    }

    it('omits the shift end when the rotation source published an unparseable one', async () => {
      stubRoutes(
        mkState({ rotation: mkRotation({ on_shift: true, who: 'octocat', until: 'soon' }) }),
      )
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText('On shift: octocat until')).toBeInTheDocument(),
      )
    })

    const modes: [OperatingMode, string][] = [
      ['observe', 'Observe'],
      ['propose', 'Propose'],
      ['act', 'Act'],
    ]
    for (const [mode, label] of modes) {
      it(`renders the ${mode} mode badge`, async () => {
        stubRoutes(mkState({ rotation: mkRotation({ mode }) }))
        renderWithProviders(<OpsMissionControlPage />)
        await waitFor(() => expect(screen.getByText(label)).toBeInTheDocument())
      })
    }
  })

  describe('on-call roster card', () => {
    const roster = (over: Record<string, unknown> = {}) =>
      mkState({
        rotation: mkRotation({
          on_shift: true,
          who: 'octocat',
          roster: {
            members: [
              { login: 'octocat', shifts: 2, on_call_now: true },
              { login: 'hubot', shifts: 1, on_call_now: false },
            ],
            windows: [],
            timezone: 'UTC',
            me: 'octocat',
            me_on_roster: true,
            strict_gating: true,
            leader: 'hubot',
            error: '',
            ...over,
          },
        }),
      })

    it('marks this instance, the on-call member, the leader and the shift counts', async () => {
      stubRoutes(roster())
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() => expect(screen.getByText('On-call team')).toBeInTheDocument())
      expect(screen.getByText('octocat (this instance)')).toBeInTheDocument()
      expect(screen.getByText('hubot')).toBeInTheDocument()
      expect(screen.getByText('On call now')).toBeInTheDocument()
      expect(screen.getByText('leader')).toBeInTheDocument()
      expect(screen.getByText('2 shifts')).toBeInTheDocument()
      expect(screen.getByText('1 shift')).toBeInTheDocument()
      expect(
        screen.getByText('UTC · only the on-call instance picks up work'),
      ).toBeInTheDocument()
    })

    it('shows the bare timezone when gating is lenient', async () => {
      stubRoutes(roster({ strict_gating: false }))
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() => expect(screen.getByText('UTC')).toBeInTheDocument())
    })

    it('warns that an unnamed instance will never pick up work under strict gating', async () => {
      stubRoutes(roster({ me: 'ghost', me_on_roster: false }))
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(
          screen.getByText(/This instance \(ghost\) is not named in the schedule, so under strict gating/),
        ).toBeInTheDocument(),
      )
    })

    it('notes that an unnamed instance still works when gating is lenient', async () => {
      stubRoutes(roster({ me: 'ghost', me_on_roster: false, strict_gating: false }))
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(
          screen.getByText(/Strict gating is off, so it still picks up work/),
        ).toBeInTheDocument(),
      )
    })

    it('warns when no login could be resolved and surfaces a schedule error', async () => {
      stubRoutes(roster({ me: '', error: 'duplicate who: key on line 12' }))
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText(/No GitHub login resolved for this instance/)).toBeInTheDocument(),
      )
      expect(
        screen.getByText('Schedule problem: duplicate who: key on line 12'),
      ).toBeInTheDocument()
    })
  })

  describe('knowledge ledger table', () => {
    it('explains itself when the ledger is empty', async () => {
      stubRoutes()
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText(/Each investigation that finds a reusable fix/)).toBeInTheDocument(),
      )
    })

    it('renders a row per entry with an em dash for a clean record', async () => {
      stubRoutes(mkState(), [mkEntry()])
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() => expect(screen.getByText('Pattern')).toBeInTheDocument())
      expect(screen.getByText('Fast path')).toBeInTheDocument()
      expect(screen.getByText('4×')).toBeInTheDocument()
      expect(screen.getByText('—')).toBeInTheDocument()
      expect(screen.getByText('unlocked')).toBeInTheDocument()
      expect(screen.getByText(/means an investigation may propose this fix directly/)).toBeInTheDocument()
    })

    it('marks an unproven entry locked', async () => {
      stubRoutes(mkState(), [mkEntry({ trust: 'observed' })])
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() => expect(screen.getByText('locked')).toBeInTheDocument())
      expect(screen.getByText('observed')).toBeInTheDocument()
    })

    it('reports one refuted entry the top rows may not show', async () => {
      stubRoutes(mkState({ ledger: mkStats({ demoted: 1, total: 7 }) }), [mkEntry()])
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText(/1 of 7 entry has been refuted/)).toBeInTheDocument(),
      )
    })

    it('reports several refuted entries with the plural wording', async () => {
      stubRoutes(mkState({ ledger: mkStats({ demoted: 3, total: 9 }) }), [mkEntry()])
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText(/3 of 9 entries have been refuted/)).toBeInTheDocument(),
      )
    })
  })

  describe('closed postmortems', () => {
    const closedIncident = mkIncident({
      incident_id: 'omc-9001',
      status: 'resolved',
      signal: mkSignal({ id: 'sig-9', title: 'Disk filled on the build host' }),
    })

    it('says nothing has closed yet when the history is empty', async () => {
      stubRoutes()
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText(/Nothing has closed yet/)).toBeInTheDocument(),
      )
      expect(screen.getByText('Closed — postmortems')).toBeInTheDocument()
    })

    it('surfaces a failed history fetch', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockRejectedValue(new Error('history unreadable'))
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getByText('history unreadable')).toBeInTheDocument(),
      )
    })

    it('lists closed incidents and says when the server clipped the list', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockResolvedValue({
        incidents: [
          closedIncident,
          mkIncident({ incident_id: 'omc-9002', status: 'dispatched' }),
        ],
        truncated: true,
        total: 640,
      })
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getAllByTestId('omc-closed-row')).toHaveLength(1),
      )
      expect(screen.getByText('Disk filled on the build host')).toBeInTheDocument()
      expect(
        screen.getByText(/Showing the 1 closed incident in the most recent/),
      ).toBeInTheDocument()
      expect(
        screen.getByText(/2 of 640 — older ones are on disk but not in this list/),
      ).toBeInTheDocument()
    })

    it('reads the postmortem on expand, with its path and a copy button', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [closedIncident] })
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText },
        configurable: true,
      })
      vi.mocked(opsApi.incident).mockResolvedValue({
        incident: closedIncident,
        log: '# Postmortem\n\nThe build host ran out of disk.',
        log_path: '/home/u/.kiro/crew/ops/incidents/omc-9001.md',
      })
      renderWithProviders(<OpsMissionControlPage />)
      const row = await waitFor(() => screen.getAllByTestId('omc-closed-row')[0])
      fireEvent.click(row)
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /Copy postmortem/ })).toBeInTheDocument(),
      )
      expect(
        screen.getByText('/home/u/.kiro/crew/ops/incidents/omc-9001.md'),
      ).toBeInTheDocument()
      expect(screen.getByText(/The build host ran out of disk/)).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: /Copy postmortem/ }))
      await waitFor(() =>
        expect(writeText).toHaveBeenCalledWith('# Postmortem\n\nThe build host ran out of disk.'),
      )
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /Copied/ })).toBeInTheDocument(),
      )
      // Collapsing the row unmounts the postmortem again.
      fireEvent.click(screen.getAllByTestId('omc-closed-row')[0])
      await waitFor(() => expect(screen.queryByText(/Copy postmortem/)).toBeNull())
    })

    it('stays silent when the clipboard refuses the copy', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [closedIncident] })
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: vi.fn().mockRejectedValue(new Error('permission denied')) },
        configurable: true,
      })
      vi.mocked(opsApi.incident).mockResolvedValue({
        incident: closedIncident,
        log: 'the record',
        log_path: '',
      })
      renderWithProviders(<OpsMissionControlPage />)
      const row = await waitFor(() => screen.getAllByTestId('omc-closed-row')[0])
      fireEvent.click(row)
      const copy = await waitFor(() =>
        screen.getByRole('button', { name: /Copy postmortem/ }),
      )
      fireEvent.click(copy)
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /Copy postmortem/ })).toBeInTheDocument(),
      )
      expect(screen.queryByText(/Copied/)).toBeNull()
    })

    it('distinguishes an incident that never got a postmortem from an empty one', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [closedIncident] })
      vi.mocked(opsApi.incident).mockResolvedValue({
        incident: closedIncident,
        log: '',
        log_path: '',
      })
      renderWithProviders(<OpsMissionControlPage />)
      const row = await waitFor(() => screen.getAllByTestId('omc-closed-row')[0])
      fireEvent.click(row)
      await waitFor(() =>
        expect(
          screen.getByText(/No postmortem was written for this incident/),
        ).toBeInTheDocument(),
      )
    })

    it('surfaces a failed postmortem read', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [closedIncident] })
      vi.mocked(opsApi.incident).mockRejectedValue(new Error('artifact missing on disk'))
      renderWithProviders(<OpsMissionControlPage />)
      const row = await waitFor(() => screen.getAllByTestId('omc-closed-row')[0])
      fireEvent.click(row)
      await waitFor(() =>
        expect(screen.getByText('artifact missing on disk')).toBeInTheDocument(),
      )
    })

    it('shows a per-status label and age on the closed row', async () => {
      vi.mocked(opsApi.state).mockResolvedValue(mkState())
      vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [], stats: mkStats() })
      vi.mocked(opsApi.incidents).mockResolvedValue({
        incidents: [
          closedIncident,
          mkIncident({
            incident_id: 'omc-9003',
            status: 'escalated',
            updated_at: agoIso(7200),
            signal: mkSignal({ id: 'sig-esc', title: 'Paged out', severity: 'info' }),
          }),
        ],
      })
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getAllByTestId('omc-closed-row')).toHaveLength(2),
      )
      expect(screen.getByText('Escalated')).toBeInTheDocument()
      expect(screen.getByText('info')).toBeInTheDocument()
      expect(screen.getByText('2h')).toBeInTheDocument()
    })
  })

  describe('row status icons', () => {
    const statuses: IncidentStatus[] = [
      'investigating',
      'dispatched',
      'needs_human',
      'resolved',
      'escalated',
      'stale',
      'unclaimed',
    ]

    it('renders one row per status without falling over on any of them', async () => {
      stubRoutes(
        mkState({
          incidents: statuses.map((status, i) =>
            mkIncident({ incident_id: `omc-${i}`, status }),
          ),
        }),
      )
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() =>
        expect(screen.getAllByTestId('omc-incident-row')).toHaveLength(statuses.length),
      )
      expect(screen.getByText('Stale')).toBeInTheDocument()
      expect(screen.getByText('Unclaimed')).toBeInTheDocument()
      expect(screen.getByText('Needs human')).toBeInTheDocument()
    })

    it('degrades to the plain status for a blocked reason it has no wording for', async () => {
      stubRoutes(
        mkState({
          incidents: [
            mkIncident({
              // A future backend value the catalog has no copy for yet. The row
              // shows the STATUS in that case — `blockedLabel`'s
              // underscore-stripping fallback is unreachable from here, because
              // `statusText` only consults it for a reason it already knows.
              blocked_reason: 'awaiting_parts' as Incident['blocked_reason'],
            }),
          ],
        }),
      )
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() => expect(screen.getByText('Investigating')).toBeInTheDocument())
      expect(screen.queryByText(/awaiting.parts/)).toBeNull()
      // It still counts as work waiting on a person, from `blocked_reason` alone.
      const card = screen.getByText('Waiting on you').closest('[data-testid="stat-card"]')
      expect(card?.querySelector('[data-testid="stat-card-value"]')?.textContent).toBe('1')
    })

    it('renders every severity variant', async () => {
      stubRoutes(
        mkState({
          incidents: [
            mkIncident({ incident_id: 'a', signal: mkSignal({ severity: 'critical' }) }),
            mkIncident({ incident_id: 'b', signal: mkSignal({ severity: 'warning' }) }),
            mkIncident({ incident_id: 'c', signal: mkSignal({ severity: 'info' }) }),
          ],
        }),
      )
      renderWithProviders(<OpsMissionControlPage />)
      await waitFor(() => expect(screen.getByText('critical')).toBeInTheDocument())
      expect(screen.getByText('warning')).toBeInTheDocument()
      expect(screen.getByText('info')).toBeInTheDocument()
    })

    it('renders an em dash for an incident whose signal carries no resource', async () => {
      stubRoutes(
        mkState({ incidents: [mkIncident({ signal: mkSignal({ resource: '' }) })] }),
      )
      renderWithProviders(<OpsMissionControlPage />)
      await expandFirstRow()
      expect(screen.getByText('Resource')).toBeInTheDocument()
      expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })
})
