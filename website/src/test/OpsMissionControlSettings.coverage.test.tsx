/**
 * First RENDER coverage for Ops Mission Control → Settings.
 *
 * `opsMissionControl.test.ts` already guards this panel, but it does so by READING
 * the source: it never mounts the component, so every conditional branch in the six
 * cards was unexecuted. That is the gap this file closes — the copy assertions there
 * prove a sentence is written, these prove it reaches the screen for the state it
 * describes and not for the states it does not.
 *
 * Shape of the harness, matching AgentsPageInspector.test.tsx / ArtifactDeployPage.test.tsx:
 *
 *  - The panel's only seam to the gateway is `./api`, so `opsApi` is mocked whole and
 *    nothing here touches the network. Three queries (`providers` / `rotation` / `state`)
 *    and four writers (`putSettings` / `putProviderConfig` / `putSecret` / `deleteSecret`).
 *  - A fresh QueryClient per render with `retry: false`, so a rejected mutation surfaces
 *    its message on the first attempt instead of after three backoff waits — no test here
 *    depends on elapsed time.
 *  - `fireEvent` rather than `userEvent`: every interaction the panel offers is a click, a
 *    change or an Enter keydown, and keeping them synchronous keeps each trigger explicit.
 *  - Queries go through roles and visible copy. The field rows are `<label htmlFor>`
 *    wrappers holding an input AND a Save button, so a label matches two controls — the
 *    `field` helper below picks the input, and `saveIn` scopes the button to its own row.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type {
  AutonomyRule,
  BoardState,
  LedgerSyncStatus,
  NotifyOutStatus,
  ProviderInfo,
  RotationInfo,
  RotationRoster,
  SlackOutStatus,
  SweepWindows,
} from '../apps/ops-mission-control/api'

const mockApi = vi.hoisted(() => ({
  providers: vi.fn(),
  rotation: vi.fn(),
  state: vi.fn(),
  putSettings: vi.fn(),
  putProviderConfig: vi.fn(),
  putSecret: vi.fn(),
  deleteSecret: vi.fn(),
}))
vi.mock('../apps/ops-mission-control/api', () => ({ opsApi: mockApi }))

const SettingsPanel = (await import('../apps/ops-mission-control/SettingsPanel')).default

/* ── fixtures ─────────────────────────────────────────────────────────────── */

const provider = (over: Partial<ProviderInfo> = {}): ProviderInfo => ({
  id: 'cloudwatch',
  display_name: 'CloudWatch',
  roles: ['signal'],
  configured: true,
  config_fields: ['enabled', 'region'],
  secret_fields: [],
  detail: 'Reads alarm state from your account.',
  config: { enabled: true, region: 'us-east-1' },
  secrets: {},
  ...over,
})

const ROTATION: RotationInfo = {
  on_shift: true,
  who: 'octocat',
  until: '',
  unknown: false,
  tiers: {},
  armed_crons: [],
  tier_crons: {},
  mode: 'observe',
  rules: 0,
  primary: false,
  modes_available: ['observe', 'propose', 'act'],
}

const roster = (over: Partial<RotationRoster> = {}): RotationRoster => ({
  members: [],
  windows: [],
  timezone: 'UTC',
  me: 'octocat',
  me_on_roster: true,
  strict_gating: true,
  leader: '',
  error: '',
  ...over,
})

const sweep = (over: Partial<SweepWindows> = {}): SweepWindows => ({
  max_claims_per_cycle: 3,
  stale_after_secs: 7200,
  needs_human_stale_after_secs: 43200,
  needs_human_derived: true,
  ...over,
})

const ledgerSync = (over: Partial<LedgerSyncStatus> = {}): LedgerSyncStatus => ({
  enabled: true,
  remote: 'git@github.com:org/ops-memory.git',
  branch: 'main',
  local_branch: 'main',
  branch_matches: true,
  detached: false,
  initialized: true,
  ready: true,
  conflict: false,
  schedule_conflict: false,
  detail: '',
  ...over,
})

const slack = (over: Partial<SlackOutStatus> = {}): SlackOutStatus => ({
  enabled: false,
  channel: '',
  slack_available: true,
  ready: false,
  detail: '',
  ...over,
})

const notify = (over: Partial<NotifyOutStatus> = {}): NotifyOutStatus => ({
  enabled: false,
  bus_available: true,
  ready: false,
  detail: '',
  channels: [],
  ...over,
})

const boardState = (over: Partial<BoardState> = {}): BoardState => ({
  incidents: [],
  counts: {},
  providers: [],
  rotation: ROTATION,
  ledger: {
    total: 0,
    verified: 0,
    high_confidence: 0,
    total_uses: 0,
    proven: 0,
    demoted: 0,
    total_misses: 0,
  },
  webhook_queue: 0,
  ...over,
})

/** Mount the panel; every query is already primed by `beforeEach`. */
function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <SettingsPanel />
    </QueryClientProvider>,
  )
}

/**
 * The `<input>` of the row whose label matches `text`.
 *
 * Every field row nests BOTH its input and its Save button inside one `<label htmlFor>`, so
 * two labelled controls answer to the same accessible name and a bare `getByLabelText` fails
 * with "found multiple elements". The tag filter picks the one a value can be typed into.
 */
const field = (text: RegExp): HTMLInputElement => {
  const input = screen.getAllByLabelText(text).find((n) => n.tagName === 'INPUT')
  if (!input) throw new Error(`no input labelled ${String(text)}`)
  return input as HTMLInputElement
}

/** `field`, awaited — for the first lookup after a mount, before the queries have settled. */
const findField = async (text: RegExp): Promise<HTMLInputElement> => {
  await waitFor(() => expect(screen.getAllByLabelText(text).length).toBeGreaterThan(0))
  return field(text)
}

/** The row itself, so its own commit button can be addressed without colliding with the
 *  eight other Save buttons on the page. */
const row = (text: RegExp) => field(text).closest('label') as HTMLElement

/**
 * The commit button of one row.
 *
 * Queried as "the row's only button" rather than by name: the button is nested inside a
 * `<label htmlFor>`, so its accessible name is the label's WHOLE text ("RepositorySave")
 * and `{ name: 'Save' }` matches nothing. Its visible wording is asserted with
 * `toHaveTextContent` where that wording is the point (Save vs Replace).
 */
const commitIn = (label: RegExp) => within(row(label)).getByRole('button')

beforeEach(() => {
  Object.values(mockApi).forEach((fn) => fn.mockReset())
  mockApi.providers.mockResolvedValue({ providers: [] })
  mockApi.rotation.mockResolvedValue({ ...ROTATION })
  mockApi.state.mockResolvedValue(boardState())
  mockApi.putSettings.mockResolvedValue({ ok: true })
  mockApi.putProviderConfig.mockResolvedValue({ ok: true })
  mockApi.putSecret.mockResolvedValue({ ok: true })
  mockApi.deleteSecret.mockResolvedValue({ ok: true })
})

afterEach(() => cleanup())

/* ── autonomy ceiling ─────────────────────────────────────────────────────── */

describe('the autonomy ceiling', () => {
  it('opens on the mode the gateway reports, with that mode\'s consequence spelled out', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, mode: 'propose' })
    renderPanel()

    expect(
      await screen.findByText(/drafts the acknowledge \/ resolve \/ comment action/),
    ).toBeInTheDocument()
    // Observe's paragraph belongs to a DIFFERENT mode; only the active one renders.
    expect(screen.queryByText(/Writes nothing to any provider/)).not.toBeInTheDocument()
  })

  it('writes the picked mode through the settings route', async () => {
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: /Act/ }))
    await waitFor(() => expect(mockApi.putSettings).toHaveBeenCalledWith({ mode: 'act' }))
  })

  it('warns, on act, that the mode alone grants nothing', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, mode: 'act' })
    renderPanel()
    expect(
      await screen.findByText(/Act only takes effect for signals matched by a rule/),
    ).toBeInTheDocument()
  })

  it('does not show that warning below act, where it would describe nothing', async () => {
    renderPanel()
    await screen.findByText(/Writes nothing to any provider/)
    expect(
      screen.queryByText(/Act only takes effect for signals matched by a rule/),
    ).not.toBeInTheDocument()
  })

  it('surfaces a refused write instead of leaving the click silent', async () => {
    mockApi.putSettings.mockRejectedValue(new Error('mode not accepted: act'))
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: /Act/ }))
    expect(await screen.findByText('mode not accepted: act')).toBeInTheDocument()
  })
})

/* ── act rules ────────────────────────────────────────────────────────────── */

describe('the act-rules card', () => {
  const withRules = (rules: AutonomyRule[]) =>
    mockApi.rotation.mockResolvedValue({ ...ROTATION, mode: 'act', rules_detail: rules })

  it('stays absent on a gateway that reports only a rule count', async () => {
    renderPanel()
    await screen.findByText('Providers')
    expect(screen.queryByText('Act rules')).not.toBeInTheDocument()
  })

  it('says there are no rules yet, and points at the form below', async () => {
    withRules([])
    renderPanel()
    expect(await screen.findByText(/No rules yet/)).toBeInTheDocument()
  })

  it('lists an existing grant in full, not as a count', async () => {
    withRules([
      { source: 'cloudwatch', mode: 'act', resource_glob: 'prod-*', actions: ['ack'] },
      { source: 'pagerduty', mode: 'act', resource_glob: 'db-*' },
    ])
    renderPanel()

    expect(await screen.findByText('prod-*')).toBeInTheDocument()
    expect(screen.getByText('db-*')).toBeInTheDocument()
    expect(screen.getByText('ack')).toBeInTheDocument()
    // A rule with no explicit action list authorizes every action; saying so beats a blank.
    expect(screen.getByText('all actions')).toBeInTheDocument()
  })

  it('revokes the row that was clicked and keeps the rest', async () => {
    const keep: AutonomyRule = { source: 'pagerduty', mode: 'act', resource_glob: 'db-*' }
    withRules([{ source: 'cloudwatch', mode: 'act', resource_glob: 'prod-*' }, keep])
    renderPanel()

    const revokes = await screen.findAllByRole('button', { name: 'Revoke rule' })
    fireEvent.click(revokes[0])
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ autonomy_rules: [keep] }),
    )
  })

  it('offers no form at all when no configured signal source exists', async () => {
    withRules([])
    mockApi.providers.mockResolvedValue({
      providers: [
        provider({ configured: false }),
        provider({ id: 'schedule-file', display_name: 'Schedule file', roles: ['rotation'] }),
      ],
    })
    renderPanel()

    expect(await screen.findByText(/Connect a signal source first/)).toBeInTheDocument()
    expect(screen.queryByText('Resource pattern')).not.toBeInTheDocument()
  })

  it('grants only once BOTH a source and a pattern are chosen', async () => {
    withRules([])
    mockApi.providers.mockResolvedValue({ providers: [provider()] })
    renderPanel()

    const glob = await findField(/Resource pattern/)
    const grant = commitIn(/Resource pattern/)
    expect(grant).toHaveTextContent('Grant')
    // A pattern with no source is not a rule; the backend would 400 it.
    fireEvent.change(glob, { target: { value: 'prod-*' } })
    expect(grant).toBeDisabled()

    fireEvent.click(screen.getByRole('combobox', { name: 'Signal source' }))
    fireEvent.click(await screen.findByRole('option', { name: 'CloudWatch' }))
    await waitFor(() => expect(grant).toBeEnabled())

    fireEvent.click(grant)
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({
        autonomy_rules: [{ source: 'cloudwatch', mode: 'act', resource_glob: 'prod-*' }],
      }),
    )
  })
})

/* ── provider rows ────────────────────────────────────────────────────────── */

describe('a provider row', () => {
  it('reports loading before the catalog lands', async () => {
    let release: (v: { providers: ProviderInfo[] }) => void = () => {}
    mockApi.providers.mockReturnValue(
      new Promise<{ providers: ProviderInfo[] }>((r) => {
        release = r
      }),
    )
    renderPanel()

    expect((await screen.findAllByText('Loading…')).length).toBeGreaterThan(0)
    release({ providers: [provider()] })
    expect(await screen.findByText('CloudWatch')).toBeInTheDocument()
  })

  it('renders readiness, roles and the adapter\'s own detail sentence', async () => {
    mockApi.providers.mockResolvedValue({
      providers: [
        provider({ roles: ['signal', 'action'] }),
        provider({ id: 'datadog', display_name: 'Datadog', configured: false, config: {} }),
      ],
    })
    renderPanel()

    expect(await screen.findByText('ready')).toBeInTheDocument()
    expect(screen.getByText('not set up')).toBeInTheDocument()
    expect(screen.getByText('signal · action')).toBeInTheDocument()
    expect(screen.getAllByText('Reads alarm state from your account.')).toHaveLength(2)
  })

  it('paints an enable toggle only for an adapter that declares the flag', async () => {
    mockApi.providers.mockResolvedValue({
      providers: [
        provider(),
        provider({
          id: 'schedule-file',
          display_name: 'Schedule file (git)',
          config_fields: [],
          config: {},
        }),
      ],
    })
    renderPanel()

    expect(await screen.findByRole('switch', { name: 'Enable CloudWatch' })).toBeInTheDocument()
    // The dead control: `PUT /providers/schedule-file/config` 400s every key the adapter
    // declares none of, so a toggle here could never latch.
    expect(
      screen.queryByRole('switch', { name: /Enable Schedule file/ }),
    ).not.toBeInTheDocument()
  })

  it('sends the enable flag when the toggle is clicked', async () => {
    mockApi.providers.mockResolvedValue({ providers: [provider({ config: { enabled: false } })] })
    renderPanel()

    fireEvent.click(await screen.findByRole('switch', { name: 'Enable CloudWatch' }))
    await waitFor(() =>
      expect(mockApi.putProviderConfig).toHaveBeenCalledWith('cloudwatch', { enabled: true }),
    )
  })

  it('hides the config fields of a declared-but-disabled adapter', async () => {
    mockApi.providers.mockResolvedValue({
      providers: [provider({ config: { enabled: false, region: 'us-east-1' } })],
    })
    renderPanel()

    await screen.findByRole('switch', { name: 'Enable CloudWatch' })
    expect(screen.queryByLabelText(/^region/)).not.toBeInTheDocument()
  })

  it('shows the config fields of an adapter that has no enable flag at all', async () => {
    // `fieldsVisible` is `enabled || !hasEnableFlag`: without the second half this row's
    // only editable control would be unreachable forever.
    mockApi.providers.mockResolvedValue({
      providers: [provider({ config_fields: ['region'], config: { region: 'eu-west-1' } })],
    })
    renderPanel()
    expect(await findField(/^region/)).toHaveValue('eu-west-1')
  })

  it('commits a changed config field on blur, and not when it is unchanged', async () => {
    mockApi.providers.mockResolvedValue({ providers: [provider()] })
    renderPanel()

    const input = await findField(/^region/)
    fireEvent.blur(input)
    expect(mockApi.putProviderConfig).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'eu-west-1' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(mockApi.putProviderConfig).toHaveBeenCalledWith('cloudwatch', {
        region: 'eu-west-1',
      }),
    )
  })

  it('reports a rejected write where the operator can see it', async () => {
    mockApi.providers.mockResolvedValue({ providers: [provider()] })
    mockApi.putProviderConfig.mockRejectedValue(new Error('unknown config key: region'))
    renderPanel()

    const input = await findField(/^region/)
    fireEvent.change(input, { target: { value: 'eu-west-1' } })
    fireEvent.blur(input)
    expect(await screen.findByText('unknown config key: region')).toBeInTheDocument()
  })
})

describe('a secret field', () => {
  const withSecret = (secrets: Record<string, string>) =>
    mockApi.providers.mockResolvedValue({
      providers: [
        provider({
          id: 'pagerduty',
          display_name: 'PagerDuty',
          config_fields: ['enabled'],
          secret_fields: ['api_key'],
          config: { enabled: true },
          secrets,
        }),
      ],
    })

  it('never pre-fills a stored value, and offers Replace instead of Save', async () => {
    withSecret({ api_key: 'set' })
    renderPanel()

    const input = await findField(/api_key/)
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('type', 'password')
    expect(input).toHaveAttribute('placeholder', 'stored — enter a new value to replace')
    expect(commitIn(/api_key/)).toHaveTextContent('Replace')
  })

  it('says the field is unset when it is, and labels the button Save', async () => {
    withSecret({})
    renderPanel()

    expect(await findField(/api_key/)).toHaveAttribute('placeholder', 'not set')
    expect(commitIn(/api_key/)).toHaveTextContent('Save')
  })

  it('refuses to submit an empty draft, then stores one and drops it again', async () => {
    withSecret({})
    renderPanel()

    const input = await findField(/api_key/)
    expect(commitIn(/api_key/)).toBeDisabled()

    fireEvent.change(input, { target: { value: 'pd-abc123' } })
    fireEvent.click(commitIn(/api_key/))
    await waitFor(() =>
      expect(mockApi.putSecret).toHaveBeenCalledWith('pagerduty', 'api_key', 'pd-abc123'),
    )
    // The draft is cleared on success so a credential does not outlive its own request.
    await waitFor(() => expect(input).toHaveValue(''))
  })

  it('offers revocation only once something is actually stored', async () => {
    withSecret({})
    renderPanel()
    await findField(/api_key/)
    expect(
      screen.queryByRole('button', { name: /Revoke stored credentials/ }),
    ).not.toBeInTheDocument()

    cleanup()
    withSecret({ api_key: 'set' })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: /Revoke stored credentials/ }))
    await waitFor(() => expect(mockApi.deleteSecret).toHaveBeenCalledWith('pagerduty'))
    // The retention boundary is disclosed beside the only control that changes it.
    expect(screen.getByText(/uninstalling this app does not delete them/)).toBeInTheDocument()
  })

  it('reports a rejected secret write', async () => {
    withSecret({})
    mockApi.putSecret.mockRejectedValue(new Error('keystone is read-only'))
    renderPanel()

    fireEvent.change(await findField(/api_key/), { target: { value: 'x' } })
    fireEvent.click(commitIn(/api_key/))
    expect(await screen.findByText('keystone is read-only')).toBeInTheDocument()
  })
})

describe('the fenced rotation identity', () => {
  const pagerduty = () =>
    mockApi.providers.mockResolvedValue({
      providers: [
        provider({ id: 'pagerduty', display_name: 'PagerDuty', config_fields: [], config: {} }),
      ],
    })

  it('renders for the adapter that consumes it, with the keystone value seeded', async () => {
    pagerduty()
    mockApi.rotation.mockResolvedValue({
      ...ROTATION,
      identities: { schedule_github_login: '', pagerduty_user_id: 'PABC123' },
    })
    renderPanel()

    const identity = await findField(/Your PagerDuty user ID/)
    await waitFor(() => expect(identity).toHaveValue('PABC123'))
  })

  it('does not render for an adapter that has no such field', async () => {
    mockApi.providers.mockResolvedValue({ providers: [provider()] })
    renderPanel()
    await screen.findByText('CloudWatch')
    expect(screen.queryByLabelText(/Your PagerDuty user ID/)).not.toBeInTheDocument()
  })

  it('writes it through PUT /settings, not through the agent-writable provider config', async () => {
    pagerduty()
    renderPanel()

    const input = await findField(/Your PagerDuty user ID/)
    expect(commitIn(/Your PagerDuty user ID/)).toBeDisabled()

    fireEvent.change(input, { target: { value: ' PXYZ789 ' } })
    fireEvent.click(commitIn(/Your PagerDuty user ID/))
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ pagerduty_user_id: 'PXYZ789' }),
    )
    expect(mockApi.putProviderConfig).not.toHaveBeenCalled()
  })

  it('commits on Enter as well as on the button', async () => {
    pagerduty()
    renderPanel()

    const input = await findField(/Your PagerDuty user ID/)
    fireEvent.change(input, { target: { value: 'PENTER' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ pagerduty_user_id: 'PENTER' }),
    )
  })

  it('writes nothing when Enter lands on an untouched identity', async () => {
    pagerduty()
    renderPanel()

    fireEvent.keyDown(await findField(/Your PagerDuty user ID/), { key: 'Enter' })
    expect(mockApi.putSettings).not.toHaveBeenCalled()
  })
})

describe('installed companions', () => {
  it('says nothing on an install that has none', async () => {
    renderPanel()
    await screen.findByText('Providers')
    expect(screen.queryByText(/adapter package/)).not.toBeInTheDocument()
  })

  it('names them, so a rejected admission is visible as a gap', async () => {
    mockApi.state.mockResolvedValue(
      boardState({ companions: [{ name: 'acme-ops', target: 'ops-mission-control' }] }),
    )
    renderPanel()
    expect(await screen.findByText(/1 adapter package installed: acme-ops/)).toBeInTheDocument()
  })
})

/* ── Slack output ─────────────────────────────────────────────────────────── */

describe('the Slack card', () => {
  it('reads off when the gateway says off, and hides the channel field', async () => {
    mockApi.state.mockResolvedValue(boardState({ slack: slack() }))
    renderPanel()

    expect(await screen.findByText('off')).toBeInTheDocument()
    expect(screen.queryByLabelText(/Channel ID/)).not.toBeInTheDocument()
  })

  it('says needs setup when enabled but not ready, and renders the backend\'s fix', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        slack: slack({ enabled: true, detail: "Kiro Crew's Slack is not connected." }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('needs setup')).toBeInTheDocument()
    expect(screen.getByText("Kiro Crew's Slack is not connected.")).toBeInTheDocument()
  })

  it('says active once the backend reports ready, and stays quiet about the fix', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        slack: slack({ enabled: true, ready: true, channel: 'C0000000001', detail: 'ok' }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('active')).toBeInTheDocument()
    expect(field(/Channel ID/)).toHaveValue('C0000000001')
    expect(screen.queryByText('ok')).not.toBeInTheDocument()
  })

  it('saves a typed channel and not an unchanged one', async () => {
    mockApi.state.mockResolvedValue(boardState({ slack: slack({ enabled: true }) }))
    renderPanel()

    const input = await findField(/Channel ID/)
    expect(commitIn(/Channel ID/)).toBeDisabled()

    fireEvent.change(input, { target: { value: ' C0123456789 ' } })
    fireEvent.click(commitIn(/Channel ID/))
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ slack_channel: 'C0123456789' }),
    )
  })

  it('writes the mirror toggle through the settings route', async () => {
    mockApi.state.mockResolvedValue(boardState({ slack: slack() }))
    renderPanel()

    fireEvent.click(await screen.findByRole('switch', { name: 'Mirror incidents to Slack' }))
    await waitFor(() => expect(mockApi.putSettings).toHaveBeenCalledWith({ slack_enabled: true }))
  })
})

/* ── desktop notifications ────────────────────────────────────────────────── */

describe('the notifications card', () => {
  it('separates an unavailable bus from a setup step, because no control fixes the former', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        notify: notify({
          enabled: true,
          bus_available: false,
          detail: 'No notification bus in this process.',
        }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('unavailable here')).toBeInTheDocument()
    expect(screen.queryByText('needs setup')).not.toBeInTheDocument()
    expect(screen.getByText('No notification bus in this process.')).toBeInTheDocument()
  })

  it('says needs setup when the bus is there and the channel is not ready', async () => {
    mockApi.state.mockResolvedValue(boardState({ notify: notify({ enabled: true }) }))
    renderPanel()
    expect(await screen.findByText('needs setup')).toBeInTheDocument()
  })

  it('declares each channel and what its edge condition is', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        notify: notify({
          enabled: true,
          ready: true,
          channels: [
            {
              id: 'waiting-on-you',
              name: 'Waiting on you',
              icon: 'UserCheck',
              default_priority: 'critical',
            },
            {
              id: 'source-health',
              name: 'Source health',
              icon: 'Radio',
              default_priority: 'passive',
            },
            { id: 'incident-released', name: 'Released', icon: 'Clock', default_priority: 'normal' },
          ],
        }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('Waiting on you')).toBeInTheDocument()
    expect(
      screen.getByText(/Fires the moment an incident starts waiting on a person/),
    ).toBeInTheDocument()
    expect(screen.getByText(/not again while it stays down/)).toBeInTheDocument()
    expect(screen.getByText(/Fires when an idle investigation is released/)).toBeInTheDocument()
    // Priority is rendered as a consequence, and only for the two that have one.
    expect(screen.getByText(/interrupts by default/)).toBeInTheDocument()
    expect(screen.getByText(/quiet, and expires on its own/)).toBeInTheDocument()
    // Mute lives centrally; the card points there rather than shipping a rival control.
    expect(screen.getByText(/Settings → Notifications/)).toBeInTheDocument()
  })

  it('still renders a channel this build has never heard of', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        notify: notify({
          enabled: true,
          ready: true,
          channels: [
            {
              id: 'brand-new',
              name: 'Brand new',
              icon: 'Nonexistent',
              default_priority: 'normal',
            },
          ],
        }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('Brand new')).toBeInTheDocument()
    expect(screen.getByText('when a state changes')).toBeInTheDocument()
  })

  it('writes its own toggle and keeps the central-rail note out of the off state', async () => {
    mockApi.state.mockResolvedValue(boardState({ notify: notify() }))
    renderPanel()

    expect(screen.queryByText(/Settings → Notifications/)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('switch', { name: 'Notify me on state changes' }))
    await waitFor(() => expect(mockApi.putSettings).toHaveBeenCalledWith({ notify_enabled: true }))
  })
})

/* ── shared team memory ───────────────────────────────────────────────────── */

describe('the shared-memory card', () => {
  it('seeds both fields from the server and saves each separately', async () => {
    mockApi.state.mockResolvedValue(boardState({ ledger_sync: ledgerSync() }))
    renderPanel()

    const remote = await findField(/^Repository/)
    await waitFor(() => expect(remote).toHaveValue('git@github.com:org/ops-memory.git'))
    expect(commitIn(/^Repository/)).toBeDisabled()

    fireEvent.change(remote, { target: { value: ' git@github.com:org/other.git ' } })
    fireEvent.keyDown(remote, { key: 'Enter' })
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({
        ledger_sync_remote: 'git@github.com:org/other.git',
      }),
    )
  })

  it('never posts an empty branch, because the backend defaults only on an absent key', async () => {
    mockApi.state.mockResolvedValue(boardState({ ledger_sync: ledgerSync({ branch: 'release' }) }))
    renderPanel()

    const branch = await findField(/^Branch/)
    await waitFor(() => expect(branch).toHaveValue('release'))
    fireEvent.change(branch, { target: { value: '   ' } })
    expect(commitIn(/^Branch/)).toBeDisabled()

    fireEvent.change(branch, { target: { value: 'main' } })
    fireEvent.click(commitIn(/^Branch/))
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ ledger_sync_branch: 'main' }),
    )

    fireEvent.change(branch, { target: { value: 'trunk' } })
    fireEvent.keyDown(branch, { key: 'Enter' })
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ ledger_sync_branch: 'trunk' }),
    )
  })

  it('writes nothing when Enter lands on an unedited repo field', async () => {
    mockApi.state.mockResolvedValue(boardState({ ledger_sync: ledgerSync() }))
    renderPanel()

    const remote = await findField(/^Repository/)
    await waitFor(() => expect(remote).toHaveValue('git@github.com:org/ops-memory.git'))
    // A stray Enter must not repoint the whole team's repo at the value already stored.
    fireEvent.keyDown(remote, { key: 'Enter' })
    fireEvent.keyDown(field(/^Branch/), { key: 'Enter' })
    expect(mockApi.putSettings).not.toHaveBeenCalled()
  })

  it('hides a credential embedded in the remote it paints', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        ledger_sync: ledgerSync({ remote: 'https://user:ghp_secret@github.com/org/repo.git' }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('https://github.com/org/repo.git')).toBeInTheDocument()
    expect(screen.queryByText(/ghp_secret/)).not.toBeInTheDocument()
  })

  it('ranks a schedule conflict above every other state and calls it an error', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        ledger_sync: ledgerSync({
          conflict: true,
          schedule_conflict: true,
          branch_matches: false,
          detail: 'rotation.yaml holds conflict markers.',
        }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('schedule conflict')).toBeInTheDocument()
    expect(screen.queryByText('ledger conflict')).not.toBeInTheDocument()
    expect(screen.getByText('rotation.yaml holds conflict markers.')).toBeInTheDocument()
    expect(screen.getByText(/Pushes stay refused until/)).toBeInTheDocument()
  })

  it('reports a ledger conflict when that is the worst thing wrong', async () => {
    mockApi.state.mockResolvedValue(
      boardState({ ledger_sync: ledgerSync({ conflict: true, detail: 'ledger.jsonl has markers.' }) }),
    )
    renderPanel()
    expect(await screen.findByText('ledger conflict')).toBeInTheDocument()
  })

  it('names a drifted local branch without claiming the exchange stopped', async () => {
    mockApi.state.mockResolvedValue(
      boardState({ ledger_sync: ledgerSync({ local_branch: 'legacy-default', branch_matches: false }) }),
    )
    renderPanel()

    expect(await screen.findByText('wrong local branch')).toBeInTheDocument()
    expect(screen.getByText('This repo is on')).toBeInTheDocument()
    expect(screen.getByText('legacy-default')).toBeInTheDocument()
    expect(screen.getByText(/still being exchanged/)).toBeInTheDocument()
    expect(screen.getByText(/The next sync moves it across by itself/)).toBeInTheDocument()
  })

  it('distinguishes a detached HEAD, which the next sync will NOT repair', async () => {
    mockApi.state.mockResolvedValue(
      boardState({
        ledger_sync: ledgerSync({ branch_matches: false, detached: true, local_branch: '' }),
      }),
    )
    renderPanel()

    expect(await screen.findByText('detached HEAD')).toBeInTheDocument()
    expect(screen.getByText('no branch (detached HEAD)')).toBeInTheDocument()
    expect(screen.getByText(/A detached HEAD is left alone on purpose/)).toBeInTheDocument()
  })

  it('says syncing on a healthy initialized repo, with nothing to fix', async () => {
    mockApi.state.mockResolvedValue(boardState({ ledger_sync: ledgerSync({ detail: 'up to date' }) }))
    renderPanel()

    expect(await screen.findByText('syncing')).toBeInTheDocument()
    expect(screen.queryByText('up to date')).not.toBeInTheDocument()
  })

  it('says ready before the repo exists, and needs setup when it is not ready', async () => {
    mockApi.state.mockResolvedValue(boardState({ ledger_sync: ledgerSync({ initialized: false }) }))
    renderPanel()
    expect(await screen.findByText('ready')).toBeInTheDocument()

    cleanup()
    mockApi.state.mockResolvedValue(
      boardState({
        ledger_sync: ledgerSync({ ready: false, initialized: false, detail: 'No remote set.' }),
      }),
    )
    renderPanel()
    expect(await screen.findByText('needs setup')).toBeInTheDocument()
    expect(screen.getByText('No remote set.')).toBeInTheDocument()
  })

  it('writes the share toggle through the settings route', async () => {
    mockApi.state.mockResolvedValue(
      boardState({ ledger_sync: ledgerSync({ enabled: false, ready: false }) }),
    )
    renderPanel()

    fireEvent.click(
      await screen.findByRole('switch', { name: 'Share the knowledge ledger with my team' }),
    )
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ ledger_sync_enabled: true }),
    )
  })
})

/* ── on-call schedule ─────────────────────────────────────────────────────── */

describe('the on-call schedule card', () => {
  const scheduleProvider = () =>
    mockApi.providers.mockResolvedValue({
      providers: [
        provider({
          id: 'schedule-file',
          display_name: 'Schedule file (git)',
          roles: ['rotation'],
          config_fields: [],
          config: {},
        }),
      ],
    })

  it('warns while sharing is off, because the schedule then reaches nobody', async () => {
    renderPanel()
    expect(await screen.findByText(/Sharing is not set up above/)).toBeInTheDocument()
  })

  it('drops that warning once sync is ready', async () => {
    mockApi.state.mockResolvedValue(boardState({ ledger_sync: ledgerSync() }))
    renderPanel()
    await screen.findByText('syncing')
    expect(screen.queryByText(/Sharing is not set up above/)).not.toBeInTheDocument()
  })

  it('shows the expected file shape, which lived only in a Python docstring', async () => {
    renderPanel()
    expect(await screen.findByText('Expected shape')).toBeInTheDocument()
    expect(screen.getByText(/THROUGH that whole day/)).toBeInTheDocument()
  })

  it('reports loading rather than a fault while the catalog is absent', async () => {
    renderPanel()
    // `schedule-file` is registered unconditionally, so absence is arrival latency.
    await screen.findByText('On-call schedule')
    expect(screen.getAllByText('Loading…').length).toBeGreaterThan(0)
    expect(screen.queryByLabelText(/Your GitHub login/)).not.toBeInTheDocument()
  })

  it('seeds the login from the roster and writes it through PUT /settings', async () => {
    scheduleProvider()
    mockApi.rotation.mockResolvedValue({ ...ROTATION, roster: roster() })
    renderPanel()

    const login = await findField(/Your GitHub login/)
    await waitFor(() => expect(login).toHaveValue('octocat'))

    fireEvent.change(login, { target: { value: 'hubot' } })
    fireEvent.keyDown(login, { key: 'Enter' })
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ schedule_github_login: 'hubot' }),
    )
  })

  it('writes nothing when Enter lands on an unedited login', async () => {
    scheduleProvider()
    mockApi.rotation.mockResolvedValue({ ...ROTATION, roster: roster() })
    renderPanel()

    const login = await findField(/Your GitHub login/)
    await waitFor(() => expect(login).toHaveValue('octocat'))
    fireEvent.keyDown(login, { key: 'Enter' })
    expect(mockApi.putSettings).not.toHaveBeenCalled()
  })

  it('surfaces a refused login write', async () => {
    scheduleProvider()
    mockApi.putSettings.mockRejectedValue(new Error('login must be a GitHub handle'))
    renderPanel()

    const login = await findField(/Your GitHub login/)
    fireEvent.change(login, { target: { value: 'not a handle' } })
    fireEvent.click(commitIn(/Your GitHub login/))
    expect(await screen.findByText('login must be a GitHub handle')).toBeInTheDocument()
  })

  it('explains an unresolved login, the setup mistake that leaves an instance idle', async () => {
    mockApi.rotation.mockResolvedValue({
      ...ROTATION,
      roster: roster({ me: '', me_on_roster: false }),
    })
    renderPanel()

    expect(
      await screen.findByText(/No GitHub login resolved for this instance/),
    ).toBeInTheDocument()
  })

  it('says an unnamed instance never picks up work while strict gating is on', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, roster: roster({ me_on_roster: false }) })
    renderPanel()

    expect(await screen.findByText(/will never pick up work/)).toBeInTheDocument()
    expect(screen.queryByText(/does not defer to whoever is on call/)).not.toBeInTheDocument()
  })

  it('says the OTHER half with strict gating off: it does not defer to anyone', async () => {
    mockApi.rotation.mockResolvedValue({
      ...ROTATION,
      roster: roster({ me_on_roster: false, strict_gating: false }),
    })
    renderPanel()

    expect(await screen.findByText(/does not defer to whoever is on call/)).toBeInTheDocument()
    expect(screen.queryByText(/will never pick up work/)).not.toBeInTheDocument()
  })

  it('renders the roster\'s own parse error verbatim', async () => {
    mockApi.rotation.mockResolvedValue({
      ...ROTATION,
      roster: roster({ error: 'rotation.yaml: shift 2 has no who key' }),
    })
    renderPanel()
    expect(await screen.findByText('rotation.yaml: shift 2 has no who key')).toBeInTheDocument()
  })

  it('offers strict gating here, with the consequence of the state it is in', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, roster: roster() })
    renderPanel()

    expect(await screen.findByText(/this instance stands down/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'Only the on-call instance picks up work' }))
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ schedule_strict_gating: false }),
    )
  })

  it('states the fail-open consequence when strict gating is already off', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, roster: roster({ strict_gating: false }) })
    renderPanel()
    expect(await screen.findByText(/this instance works anyway/)).toBeInTheDocument()
  })
})

/* ── instance ─────────────────────────────────────────────────────────────── */

describe('the instance card', () => {
  it('writes the nightly-maintenance flag from the reported state', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, primary: true })
    renderPanel()

    const toggle = await screen.findByRole('switch', {
      name: 'Run nightly ledger maintenance on this instance',
    })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ primary_instance: false }),
    )
  })
})

/* ── heartbeat pacing ─────────────────────────────────────────────────────── */

describe('the heartbeat-pacing card', () => {
  it('refuses to invent defaults a gateway did not report', async () => {
    renderPanel()

    expect(await screen.findByText(/Not reported by this gateway/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Release an investigation after/)).not.toBeInTheDocument()
  })

  it('shows the stored seconds as the minutes an operator tunes in', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, sweep: sweep() })
    renderPanel()

    expect(await findField(/Release an investigation after/)).toHaveValue('120')
    expect(field(/Release a question after/)).toHaveValue('720')
    // 43200 read as minutes is the confusion the card exists to remove.
    expect(screen.getByText('2h / 12h')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('distinguishes a derived window from a pinned one', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, sweep: sweep() })
    renderPanel()
    expect(await screen.findByText(/derived from the window above/)).toBeInTheDocument()

    cleanup()
    mockApi.rotation.mockResolvedValue({
      ...ROTATION,
      sweep: sweep({ needs_human_derived: false, needs_human_stale_after_secs: 3600 }),
    })
    renderPanel()
    expect(await screen.findByText(/Pinned at 1h/)).toBeInTheDocument()
  })

  it('renders an unreported window as a dash rather than as an immediate release', async () => {
    mockApi.rotation.mockResolvedValue({
      ...ROTATION,
      sweep: sweep({ stale_after_secs: 0, needs_human_stale_after_secs: 0 }),
    })
    renderPanel()

    expect(await screen.findByText('— / —')).toBeInTheDocument()
    // 0 is not a value to edit back into the form either.
    expect(field(/Release an investigation after/)).toHaveValue('')
  })

  it('keeps a value the backend would 400 off the wire', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, sweep: sweep() })
    renderPanel()

    const input = await findField(/Release an investigation after/)
    for (const bad of ['0', '-5', '1.5', 'abc', '']) {
      fireEvent.change(input, { target: { value: bad } })
      expect(commitIn(/Release an investigation after/)).toBeDisabled()
    }
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mockApi.putSettings).not.toHaveBeenCalled()
  })

  it('saves minutes as seconds, through each row\'s own button', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, sweep: sweep() })
    renderPanel()

    const stale = await findField(/Release an investigation after/)
    fireEvent.change(stale, { target: { value: '30' } })
    fireEvent.click(commitIn(/Release an investigation after/))
    await waitFor(() => expect(mockApi.putSettings).toHaveBeenCalledWith({ stale_after_secs: 1800 }))

    const question = field(/Release a question after/)
    fireEvent.change(question, { target: { value: '90' } })
    fireEvent.click(commitIn(/Release a question after/))
    await waitFor(() =>
      expect(mockApi.putSettings).toHaveBeenCalledWith({ needs_human_stale_after_secs: 5400 }),
    )
  })

  it('leaves a key alone when its own field is untouched', async () => {
    mockApi.rotation.mockResolvedValue({ ...ROTATION, sweep: sweep() })
    renderPanel()

    await findField(/Release an investigation after/)
    expect(commitIn(/Release a question after/)).toBeDisabled()
    fireEvent.keyDown(field(/Release a question after/), { key: 'Enter' })
    expect(mockApi.putSettings).not.toHaveBeenCalled()
  })
})
