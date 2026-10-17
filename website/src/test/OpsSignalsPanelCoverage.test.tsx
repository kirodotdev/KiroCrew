/**
 * Render coverage for Ops Mission Control → Signals tab.
 *
 * `SignalsPanel` had no mounting test at all: every branch of the source table, the
 * three mutually exclusive trust banners, the parked/recovered cards and the claim
 * receipt were unexecuted. The panel exists to stop an operator reading silence as
 * health, so the branches that distinguish "looked and saw nothing" from "could not
 * look" are exactly the ones worth pinning to the screen.
 *
 * Harness, matching `OpsMissionControlPageCoverage.test.tsx`:
 *
 *  - `opsApi` is mocked whole while every real helper (`describeSourceHealth`,
 *    `SIGNALS_QUERY_KEY`, `WEBHOOK_QUEUE_LIMIT`) is kept, because those helpers are
 *    what decide each row's state.
 *  - The panel's `/signals` and `/state` queries are `enabled: false` by design — the
 *    tab owns an explicit "Poll now" press and reads `/state` out of the cache the
 *    board fills. Tests therefore seed the cache with `setQueryData`, which is the
 *    same path production uses, and only the poll-button test drives `refetch()`.
 *  - Fake timers with `shouldAdvanceTime`, so nothing a card schedules can fire after
 *    the environment is torn down.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'
import { renderWithProviders } from './helpers'
import {
  SIGNALS_QUERY_KEY,
  WEBHOOK_QUEUE_LIMIT,
  opsApi,
  type BoardState,
  type Evidence,
  type Incident,
  type LedgerEntry,
  type ProviderInfo,
  type Severity,
  type Signal,
  type SignalsResult,
  type SourcePollHealth,
} from '../apps/ops-mission-control/api'
import SignalsPanel from '../apps/ops-mission-control/SignalsPanel'

vi.mock('../apps/ops-mission-control/api', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../apps/ops-mission-control/api')>()
  return {
    ...actual,
    opsApi: {
      providers: vi.fn(),
      signals: vi.fn(),
      state: vi.fn(),
      claim: vi.fn(),
    },
  }
})

/* ── fixtures ─────────────────────────────────────────────────────────────── */

const agoIso = (secs: number) => new Date(Date.now() - secs * 1000).toISOString()

const mkProvider = (over: Partial<ProviderInfo> = {}): ProviderInfo => ({
  id: 'cloudwatch',
  display_name: 'CloudWatch',
  roles: ['signal'],
  configured: true,
  config_fields: [],
  secret_fields: [],
  detail: 'Reads alarm state from your account.',
  config: {},
  secrets: {},
  ...over,
})

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

const okHealth = (over: Partial<SourcePollHealth> = {}): SourcePollHealth => ({
  ok: true,
  detail: '',
  at: agoIso(30),
  ...over,
})

const mkSignalsResult = (over: Partial<SignalsResult> = {}): SignalsResult => ({
  signals: [],
  firing: [],
  cleared: [],
  suppressed: [],
  unclaimed: [],
  errors: {},
  poll_health: { cloudwatch: okHealth({ signals: 0 }) },
  all_sources_healthy: true,
  ...over,
})

const mkIncident = (over: Partial<Incident> = {}): Incident => ({
  incident_id: 'omc-0001',
  signal: mkSignal(),
  status: 'investigating',
  operating_mode: 'propose',
  claimed_at: agoIso(60),
  updated_at: agoIso(10),
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

interface ClaimEnvelope {
  incident: Incident
  matches: LedgerEntry[]
  similar: LedgerEntry[]
  exact_match_ids: string[]
  evidence: Evidence[]
  fast_path: boolean
  brief: string
}

const mkClaim = (over: Partial<ClaimEnvelope> = {}): ClaimEnvelope => ({
  incident: mkIncident(),
  matches: [],
  similar: [],
  exact_match_ids: [],
  evidence: [],
  fast_path: false,
  brief: '',
  ...over,
})

/* ── harness ──────────────────────────────────────────────────────────────── */

/** Mount with a provider list already resolved, waiting for the table to settle. */
async function renderPanel(providers: ProviderInfo[] = [mkProvider()]) {
  vi.mocked(opsApi.providers).mockResolvedValue({ providers })
  const utils = renderWithProviders(<SignalsPanel />)
  await screen.findByText('Signal sources')
  return utils
}

/** Fill the shared `/signals` cache entry, the way the board's own poll does. */
function seedSignals(client: QueryClient, over: Partial<SignalsResult> = {}) {
  act(() => {
    client.setQueryData(SIGNALS_QUERY_KEY, mkSignalsResult(over))
  })
}

/** Fill the board's `/state` entry — the panel reads `webhook_queue` from it. */
function seedBoardState(client: QueryClient, webhookQueue: number) {
  act(() => {
    client.setQueryData(['ops-mission-control', 'state'], {
      webhook_queue: webhookQueue,
    } as BoardState)
  })
}

/** Data rows of the source table, header row dropped. */
function sourceRows() {
  return screen.getAllByRole('row').slice(1)
}

describe('SignalsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('waits on the provider list before drawing a table', async () => {
    vi.mocked(opsApi.providers).mockReturnValue(
      new Promise<{ providers: ProviderInfo[] }>(() => {}),
    )
    renderWithProviders(<SignalsPanel />)
    expect(await screen.findByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('says no signal sources are registered when every provider is a sink', async () => {
    await renderPanel([mkProvider({ roles: ['sink'] })])
    expect(
      await screen.findByText('No signal sources registered.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('draws every source as unpolled before a poll, with no trust banner', async () => {
    await renderPanel([
      mkProvider(),
      mkProvider({ id: 'webhook', display_name: 'Inbound webhook' }),
      mkProvider({ id: 'zabbix', display_name: 'Zabbix', configured: false }),
    ])
    await screen.findByText('CloudWatch')

    // The badge label only. The Last-poll cell carries the longer sentence, so an
    // exact match here counts badges and nothing else.
    expect(screen.getAllByText('not polled yet')).toHaveLength(2)
    expect(screen.getByText('not set up')).toBeInTheDocument()
    expect(
      screen.getAllByText('not polled yet — this is not the same as healthy'),
    ).toHaveLength(2)

    const [cloudwatch, , zabbix] = sourceRows()
    // Firing and Parked both refuse to print a zero for a source that did not
    // contribute; an unconfigured source has nothing to say about its last poll either.
    expect(within(cloudwatch).getAllByText('—')).toHaveLength(2)
    expect(within(zabbix).getAllByText('—')).toHaveLength(3)

    expect(screen.queryByText(/Every configured source answered/)).toBeNull()
    expect(screen.queryByText(/did not answer this poll/)).toBeNull()
    expect(screen.getByText('Poll to see what is firing.')).toBeInTheDocument()
  })

  it('vouches for a quiet board once every configured source answered', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, {
      poll_health: { cloudwatch: okHealth({ signals: 2 }) },
      firing: [mkSignal(), mkSignal({ id: 'sig-2' })],
      unclaimed: [mkSignal({ id: 'sig-2', title: 'Queue depth climbing' })],
    })

    expect(
      await screen.findByText(/Every configured source answered\./),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/A signal that is absent from this poll can be read as recovered/),
    ).toBeInTheDocument()
    expect(screen.getByText('2 signals · 30s ago')).toBeInTheDocument()
    expect(
      screen.getByText(/2 firing · 0 parked at provider · 0 cleared · 1 not yet claimed/),
    ).toBeInTheDocument()

    const [cloudwatch] = sourceRows()
    expect(within(cloudwatch).getByText('ok')).toBeInTheDocument()
    expect(within(cloudwatch).getByText('2')).toBeInTheDocument()
    expect(screen.getByText('Queue depth climbing')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Claim' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Provider' })).toHaveAttribute(
      'href',
      'https://console.example.com/alarm/1',
    )
  })

  it('qualifies the all-clear for a push source whose poll drains its spool', async () => {
    const { queryClient } = await renderPanel([
      mkProvider(),
      mkProvider({ id: 'webhook', display_name: 'Inbound webhook' }),
    ])
    seedSignals(queryClient, {
      poll_health: {
        cloudwatch: okHealth({ signals: 1 }),
        webhook: okHealth({ snapshot: false }),
      },
    })

    expect(
      await screen.findByText(
        /EXCEPT for Inbound webhook — it delivers by push into a spool that each poll empties/,
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText('· delivered by push, so an empty poll means nothing'),
    ).toBeInTheDocument()
    // A count present but absent from `signals` is "we answered", not "we saw zero".
    expect(screen.getByText('answered · 30s ago')).toBeInTheDocument()
    expect(screen.getByText('1 signal · 30s ago')).toBeInTheDocument()
  })

  it('names the sources that failed and prints each reason with its age', async () => {
    const { queryClient } = await renderPanel([
      mkProvider(),
      mkProvider({ id: 'datadog', display_name: 'Datadog' }),
      mkProvider({ id: 'zabbix', display_name: 'Zabbix' }),
    ])
    seedSignals(queryClient, {
      all_sources_healthy: false,
      errors: {
        cloudwatch: 'Backing off after 3 consecutive failures',
        datadog: 'ExpiredToken: the credential is stale',
        zabbix: 'connection refused',
      },
      poll_health: {
        cloudwatch: okHealth({ at: agoIso(7200) }),
        datadog: { ok: false, detail: 'ignored in favour of errors', at: agoIso(200000) },
        zabbix: { ok: false, detail: '', at: 'not-a-timestamp' },
      },
    })

    expect(
      await screen.findByText(
        /CloudWatch, Datadog, Zabbix did not answer this poll\./,
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/absence of a signal does NOT mean recovery/),
    ).toBeInTheDocument()

    // A throttled skip is not an operator error, but it contributed nothing either.
    expect(screen.getByText('backing off')).toBeInTheDocument()
    expect(
      screen.getByText('Backing off after 3 consecutive failures (2h ago)'),
    ).toBeInTheDocument()
    expect(screen.getAllByText('failed')).toHaveLength(2)
    expect(
      screen.getByText('ExpiredToken: the credential is stale (2d ago)'),
    ).toBeInTheDocument()
    // An unparseable timestamp yields no age rather than an invented one — but the
    // parentheses are emitted from the truthiness of `status.at`, not from `age()`
    // returning something, so the row prints an empty pair. Asserted as it renders.
    expect(screen.getByText('connection refused ()')).toBeInTheDocument()
  })

  it('does not accuse a source of failing when nothing is configured', async () => {
    const { queryClient } = await renderPanel([
      mkProvider({ configured: false }),
      mkProvider({ id: 'zabbix', display_name: 'Zabbix', configured: false }),
    ])
    seedSignals(queryClient, { all_sources_healthy: false, poll_health: {} })

    expect(
      await screen.findByText(/No signal source is configured, so nothing was polled/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/did not answer this poll/)).toBeNull()
  })

  it('falls back to an unnamed warning when the failing source cannot be identified', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, {
      all_sources_healthy: false,
      poll_health: { cloudwatch: okHealth({ signals: 1 }) },
    })

    expect(
      await screen.findByText(/At least one source did not answer this poll\./),
    ).toBeInTheDocument()
  })

  it('disables the poll button while polling and surfaces a poll failure', async () => {
    await renderPanel()
    let reject: (err: Error) => void = () => {}
    vi.mocked(opsApi.signals).mockReturnValue(
      new Promise<SignalsResult>((_, rej) => {
        reject = rej
      }),
    )

    const button = screen.getByRole('button', { name: /Poll now/ })
    fireEvent.click(button)

    const polling = await screen.findByRole('button', { name: /Polling…/ })
    expect(polling).toBeDisabled()

    await act(async () => {
      reject(new Error('the /signals route refused the poll'))
      await Promise.resolve()
    })

    expect(
      await screen.findByText('the /signals route refused the poll', undefined, {
        timeout: 5_000,
      }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Poll now/ })).toBeEnabled()
  })

  it('reports an exact ledger match and a cleared fast path on the claim receipt', async () => {
    const { queryClient } = await renderPanel()
    const signal = mkSignal()
    seedSignals(queryClient, { unclaimed: [signal] })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Claim' })).toBeInTheDocument(),
    )
    vi.mocked(opsApi.claim).mockResolvedValue(
      mkClaim({
        matches: [mkEntry()],
        similar: [mkEntry({ entry_id: 'entry-ccccdddd' })],
        exact_match_ids: ['entry-aaaabbbb'],
        fast_path: true,
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Claim' }))

    expect(
      await screen.findByText('Claimed omc-0001.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    expect(vi.mocked(opsApi.claim)).toHaveBeenCalledWith(signal)
    expect(
      screen.getByText(
        /1 ledger entry matched — 1 on this provider's own identity, which is an exact match\./,
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/A proven pattern matched, so the agent may propose its fix directly/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/1 similar lesson attached as context — a near-miss, not a match\./),
    ).toBeInTheDocument()
  })

  it('warns that several matches came from the shape hash and cleared no fast path', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, { unclaimed: [mkSignal()] })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Claim' })).toBeInTheDocument(),
    )
    vi.mocked(opsApi.claim).mockResolvedValue(
      mkClaim({ matches: [mkEntry(), mkEntry({ entry_id: 'entry-eeeeffff' })] }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Claim' }))

    expect(
      await screen.findByText(
        /2 ledger entries matched — all on our shape hash, which merges alarms differing only in a number\./,
        undefined,
        { timeout: 5_000 },
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/No pattern cleared the fast-path bar, so the agent must confirm/),
    ).toBeInTheDocument()
  })

  it('says the investigation starts from scratch when the ledger had nothing', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, { unclaimed: [mkSignal()] })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Claim' })).toBeInTheDocument(),
    )
    vi.mocked(opsApi.claim).mockResolvedValue(mkClaim())

    fireEvent.click(screen.getByRole('button', { name: 'Claim' }))

    expect(
      await screen.findByText(
        'Nothing in the ledger matched, so the investigation starts from scratch.',
        undefined,
        { timeout: 5_000 },
      ),
    ).toBeInTheDocument()
  })

  it('surfaces a rejected claim verbatim', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, { unclaimed: [mkSignal()] })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Claim' })).toBeInTheDocument(),
    )
    vi.mocked(opsApi.claim).mockRejectedValue(new Error('403: no rule grants a claim'))

    fireEvent.click(screen.getByRole('button', { name: 'Claim' }))

    expect(
      await screen.findByText('403: no rule grants a claim', undefined, {
        timeout: 5_000,
      }),
    ).toBeInTheDocument()
  })

  it('shows the inbound webhook spool only while something is waiting in it', async () => {
    const { queryClient } = await renderPanel()
    expect(screen.queryByText(/inbound webhook signal/)).toBeNull()

    seedBoardState(queryClient, 1)
    expect(
      await screen.findByText(
        /1 inbound webhook signal delivered and waiting for the next cycle to pick it up/,
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(/The spool is full at/)).toBeNull()
  })

  it('warns that a spool at the cap is discarding deliveries', async () => {
    const { queryClient } = await renderPanel()
    seedBoardState(queryClient, WEBHOOK_QUEUE_LIMIT)

    expect(
      await screen.findByText(
        new RegExp(`${WEBHOOK_QUEUE_LIMIT} inbound webhook signals delivered`),
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        new RegExp(`The spool is full at ${WEBHOOK_QUEUE_LIMIT}, so the oldest deliveries`),
      ),
    ).toBeInTheDocument()
  })

  it('lists parked signals with their attribution and offers no claim for them', async () => {
    const parked = (id: string, severity: Severity, over: Partial<Signal> = {}) =>
      mkSignal({ id, severity, state: 'suppressed', title: `Parked ${id}`, ...over })
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, {
      suppressed: [
        parked('s1', 'critical', { suppressed_reason: 'inhibited', suppressed_by: 'HostDown' }),
        parked('s2', 'warning', { suppressed_reason: 'inhibited' }),
        parked('s3', 'info', { suppressed_reason: 'silenced', suppressed_by: 'octocat' }),
        parked('s4', 'info', { url: 'javascript:alert(1)' }),
      ],
    })

    expect(await screen.findByText('Parked at the provider')).toBeInTheDocument()
    // The park is respected: dispatch claims only `firing`, so no button here.
    expect(screen.queryByRole('button', { name: 'Claim' })).toBeNull()
    expect(screen.getAllByText('parked')).toHaveLength(4)
    expect(screen.getAllByText('critical')).toHaveLength(1)
    expect(screen.getAllByText('warning')).toHaveLength(1)
    expect(screen.getAllByText('info')).toHaveLength(2)

    expect(
      screen.getByText(/Inhibited by HostDown — a higher-ranked alert is masking this one/),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Inhibited by another alert — a higher-ranked alert is masking this one.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Silenced by octocat — review or let the silence expire/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/This provider published no attribution, so we cannot say who parked it/),
    ).toBeInTheDocument()
    // A non-http URL is dropped rather than rendered as a link.
    expect(screen.getAllByRole('link', { name: 'Provider' })).toHaveLength(3)

    // "Nothing is firing" alone would be a lie while signals sit parked.
    expect(
      screen.getByText(
        /nothing is firing, but the parked signals above are not being investigated/,
      ),
    ).toBeInTheDocument()
    expect(screen.getByTestId('empty-state-subtitle')).toHaveTextContent(
      'Nothing is firing — but 4 signals are parked at the provider (see above) and will not be picked up.',
    )
  })

  it('explains an empty queue as already-claimed work when signals are firing', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, { firing: [mkSignal()] })

    expect(await screen.findByTestId('empty-state-title')).toHaveTextContent(
      'Nothing unclaimed',
    )
    expect(screen.getByTestId('empty-state-subtitle')).toHaveTextContent(
      'Everything currently firing already has an incident.',
    )
  })

  it('reports a genuinely quiet estate when nothing is firing or parked', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient)

    expect(await screen.findByTestId('empty-state-subtitle')).toHaveTextContent(
      'No sources are reporting a firing signal.',
    )
    expect(screen.queryByText('Parked at the provider')).toBeNull()
  })

  it('separates provider-reported recovery from a signal that merely vanished', async () => {
    const { queryClient } = await renderPanel()
    seedSignals(queryClient, {
      cleared: [mkSignal({ id: 'sig-9', title: 'Disk pressure back to normal', url: '' })],
    })

    expect(await screen.findByText('Reported recovered')).toBeInTheDocument()
    expect(screen.getByText('recovered')).toBeInTheDocument()
    expect(screen.getByText('Disk pressure back to normal')).toBeInTheDocument()
    expect(
      screen.getByText(/The provider said these are back to normal/),
    ).toBeInTheDocument()
    // No URL, so no provider link is invented for it.
    expect(screen.queryByRole('link', { name: 'Provider' })).toBeNull()
  })
})
