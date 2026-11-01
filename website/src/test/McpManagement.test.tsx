// MCP Management: the states where the page could lie to the operator.
//
// Contract under test:
// - a stub apply that PERSISTED but did not go live (200 with applied:false)
//   surfaces an error instead of drawing a live-looking switch
// - a failed server request renders its own error row, never the "none are
//   configured" empty state, which is a claim a failed request cannot make
// - an unsupported platform can still turn an inherited setting OFF; only
//   turning one ON is blocked, so nobody is trapped in a state they cannot exit
// - sharing cannot be enabled while nothing is stubbed (it would do nothing),
//   but can always be disabled
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { McpManagement } from '../pages/settings/McpManagement'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
/** The page's own English copy, so a wording change never breaks a test. */
const M = (key: string, vars?: Record<string, unknown>) => i18nT(`pages.mcpManagement.${key}`, vars)

/** Matches any text that starts the way a counted line does, whatever its numbers. */
function startsWithLine(key: string) {
  const prefix = M(key, { count: 0, done: 0, total: 0 }).split('0')[0]
  return (content: string) => content.startsWith(prefix)
}

type Server = {
  name: string
  stub: boolean
  can_stub: boolean
  in_allowlist: boolean
  entry_poolable: boolean
  agents: string[]
  transport: string
  denylisted: boolean
  pooling_blocked_by_env?: boolean
  private_backend?: boolean
}

function server(over: Partial<Server> = {}): Server {
  return {
    name: 'alpha-mcp',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['kirocrew'],
    transport: 'stdio',
    denylisted: false,
    ...over,
  }
}

/** What `GET /servers/launch` answers for a server being turned on. */
function preview(name: string, over: Record<string, unknown> = {}) {
  return {
    name,
    commands: [['npx', '-y', `${name}@1`]],
    envs: [[]],
    complete: true,
    expected_launch: `${name}-cmd:${name}-env`,
    ...over,
  }
}

/** Turn a row's share switch on once it has read the launch it pins. */
async function approveStub(name: string) {
  // Chain: servers read -> the row's launch read -> the switch enables.
  // The visible label then the server name, the same whether sharing is on or off.
  const btn = await screen.findByRole('switch', { name: `${M('share_toggle')} ${name}` }, { timeout: 5000 })
  await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
  btn.click()
}

function mount() {
  // `staleTime: Infinity` mirrors the app's real shared QueryClient
  // (`src/api/queryClient.ts`), where freshness comes from WebSocket
  // invalidation rather than from age. Without it this harness is more lenient
  // than production in exactly the direction that hides a bug: a cache-backed
  // re-read that is a no-op in the app still refetches here, so a test asserting
  // "acts on the fresh value" passes while the shipped code acts on the stale
  // one.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement initialAdvancedOpen />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const status = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
  ...over,
})

beforeEach(() => {
  vi.restoreAllMocks()
  // The hero's memory read: answered with no figures unless a test says otherwise.
  vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({ using_now_bytes: null, with_sharing_bytes: null, open_chats: 0, stale_chats: [] } as never)
  answerBatchPreviewsPerName()
  // The page-wide switch's confirm turns sharing on.
  vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ok: true, enabled: true, ping_ok: true } as never)
})
afterEach(cleanup)

describe('McpManagement', () => {
  it('reports a stub that saved but did not come up', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    // The broker failed to start: the endpoint still answers 200.
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview('alpha-mcp') as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      name: 'alpha-mcp',
      stub: true,
      applied: false,
    } as never)

    mount()
    await approveStub('alpha-mcp')

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
    })
  })

  it('asks for a restart without painting it as a failure', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    // The normal outcome: the allowlist is stored and nothing was cycled, so the
    // change is pending a restart rather than broken.
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(preview('alpha-mcp') as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      name: 'alpha-mcp',
      stub: true,
      applied: false,
      restart_required: true,
    } as never)

    mount()
    await approveStub('alpha-mcp')

    // role=status, not role=alert: an operator who is told "restart to apply"
    // has nothing to fix, and an error banner sends them looking for a fault.
    await waitFor(() => {
      expect(screen.getByRole('status')).toBeTruthy()
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('does not claim zero servers when the request failed', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockRejectedValue(new Error('boom'))

    mount()
    await waitFor(() => {
      expect(screen.queryByText(M('no_servers'))).toBeNull()
      expect(screen.getByText(M('servers_failed'))).toBeTruthy()
    })
  })

  it('lets an unsupported platform turn an inherited stub back off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ supported: false, enabled: true, stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)

    mount()
    const row = await screen.findByRole('switch', { name: `${M('share_toggle')} alpha-mcp` }, { timeout: 5000 })
    // ON and unsupported: turning it OFF must stay reachable.
    await waitFor(() => expect((row as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })

    const sharing = screen.getByRole('switch', { name: M('hero_title') })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
  })

  it('blocks enabling a stub on an unsupported platform', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ supported: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const row = await screen.findByRole('switch', { name: `${M('share_toggle')} alpha-mcp` }, { timeout: 5000 })
    expect((row as HTMLButtonElement).disabled).toBe(true)
  })

  it('refetches after a failed apply, because the setting may already be saved', async () => {
    // Both endpoints write config.json BEFORE the in-process apply, so a 500 is
    // "saved but not live". Leaving the old state on screen and saying nothing
    // was saved would hide a setting that activates on the next restart.
    const statusSpy = vi
      .spyOn(api, 'mcpGatewayStatus')
      .mockResolvedValue(status({ stub_count: 1 }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    vi.spyOn(api, 'mcpGatewayEnable').mockRejectedValue(new Error('apply failed'))

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    const before = statusSpy.mock.calls.length
    sharing.click()
    fireEvent.click(await screen.findByRole('button', { name: M('confirm_turn_on') }, { timeout: 5000 }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(statusSpy.mock.calls.length).toBeGreaterThan(before)
    })
    expect((screen.getByRole('alert').textContent ?? '').toLowerCase()).not.toContain('nothing was saved')
  })

  it('refuses to arm sharing while nothing is stubbed, but allows disarming it', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    const { unmount } = mount()
    const off = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((off as HTMLButtonElement).disabled).toBe(true), { timeout: 5000 })
    unmount()
    cleanup()

    // Already on with nothing stubbed — the state this PR removes. It must still
    // be escapable, or the operator is stuck with a switch they cannot clear.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    mount()
    const on = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    // Poll: the switch mounts before the status query resolves, and until it does
    // the page cannot know the setting is already on.
    await waitFor(() => expect((on as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
  })

  it('explains why sharing is disabled instead of just refusing the click', async () => {
    // A disabled headline control that gives no reason reads as a broken page.
    // The reason is the switch's accessible description, not a visible line.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ supported: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(true), { timeout: 5000 })
    const note = document.getElementById(sharing.getAttribute('aria-describedby') as string)
    expect(note?.textContent).toBe(M('unsupported_platform'))
  })

  it('discloses, before sharing turns on, that open chats keep their own copies', async () => {
    // The apply path leaves live sessions alone, so a toggle is NOT retroactive.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    const dialog = await screen.findByRole('dialog', undefined, { timeout: 5000 })
    expect(within(dialog).getByText(M('confirm_restart'))).toBeTruthy()
  })
})

describe('sharing assessment', () => {
  type Rec = {
    strength: string
    recommendShare: boolean
    reasons: Array<{ code: string; detail: string }>
  }

  /** A server row plus whatever verdict the gateway attached to it. */
  const withRec = (over: Partial<Server>, rec?: Rec) => ({
    ...server(over),
    ...(rec ? { recommendation: rec } : {}),
  })

  const noObjection: Rec = {
    strength: 'no_objection',
    recommendShare: false,
    reasons: [{ code: 'no_objection_found', detail: '' }],
  }

  async function openAssessment() {
    mount()
    const tab = await screen.findByRole('tab', { name: M('view_assessment') }, { timeout: 5000 })
    // Radix's tab trigger activates on mousedown (so a drag off the rail cannot
    // leave the selection half-applied); a bare `.click()` never synthesises it.
    fireEvent.mouseDown(tab)
  }

  it('shows the verdict and its reason instead of only a yes or no', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        withRec(
          { name: 'alpha-mcp' },
          {
            strength: 'disqualified',
            recommendShare: false,
            reasons: [{ code: 'rotating_secret_env', detail: 'AWS_SESSION_TOKEN' }],
          },
        ),
      ],
    } as never)
    await openAssessment()

    expect(await screen.findByText(M('assessment.strength_disqualified'), undefined, { timeout: 5000 })).toBeTruthy()
    // The translated reason AND the server's own verbatim detail, which is data
    // and must never be translated or dropped.
    expect(
      screen.getByText(M('assessment.reason_rotating_secret_env')),
    ).toBeTruthy()
    expect(screen.getByText('AWS_SESSION_TOKEN')).toBeTruthy()
  })

  it('reads a row with no verdict as not measured rather than failing', async () => {
    // An older gateway does not send `recommendation` at all. The row still has
    // to render: that is the Make Live case, not a corrupt response.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' })],
    } as never)
    await openAssessment()

    // Scoped to the pill: the view's own explanation of the state uses the
    // same words, so an unscoped text query matches twice.
    expect(await screen.findByText(M('assessment.strength_unknown'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText('alpha-mcp')).toBeTruthy()
  })

  it('does not let "no objection" read as a recommendation to share', async () => {
    // The weakest useful verdict. Its whole point is that finding nothing wrong
    // is not evidence that sharing is safe, so the label must not promise it.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, noObjection)],
    } as never)
    await openAssessment()

    // Scoped to the pill: the legend deliberately quotes this same label to
    // explain it, so an unscoped text query matches twice.
    expect(await screen.findByText(M('assessment.strength_no_objection'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(M('assessment.strength_declared'))).toBeNull()
  })

  it('flags a server sharing against evidence that argues the other way', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        withRec({ name: 'alpha-mcp', stub: true, in_allowlist: true }, {
          strength: 'refuted',
          recommendShare: false,
          reasons: [{ code: 'observed_hazard', detail: 'unroutable_notification' }],
        }),
      ],
    } as never)
    await openAssessment()

    expect(await screen.findByRole('status', undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(M('assessment.shared_without_support', { count: 1 }))).toBeTruthy()
  })

  it('stays quiet for a shared server that merely lacks an endorsement', async () => {
    // `no_objection` is the tier most healthy servers rest at, and it means
    // nothing disqualifying was found rather than something was found. Flagging
    // it would put a permanent warning over an entire fleet and train the
    // operator to ignore the page's only coloured signal.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp', stub: true, in_allowlist: true }, noObjection)],
    } as never)
    await openAssessment()

    await screen.findByText(M('assessment.strength_no_objection'), { selector: 'span' }, { timeout: 5000 })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('stays quiet for a shared server the gateway never managed to measure', async () => {
    // The backend attaches a verdict to EVERY row, so "never measured" arrives as
    // a present recommendation at `unknown`, not as an absent one. Keying the flag
    // on a falsy `recommendShare` would count it and assert a finding from a
    // measurement that never ran, while the same row's own cell reads
    // "not measured".
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        withRec({ name: 'alpha-mcp', stub: true, in_allowlist: true }, {
          strength: 'unknown',
          recommendShare: false,
          reasons: [{ code: 'not_probed', detail: '' }],
        }),
      ],
    } as never)
    await openAssessment()

    await screen.findByText(M('assessment.strength_unknown'), { selector: 'span' }, { timeout: 5000 })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('stays quiet for a shared server with no verdict at all', async () => {
    // The older-gateway shape: no `recommendation` on the row.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp', stub: true, in_allowlist: true })],
    } as never)
    await openAssessment()

    await screen.findByText(M('assessment.strength_unknown'), { selector: 'span' }, { timeout: 5000 })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('carries no row actions, because it decides nothing', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, noObjection)],
    } as never)
    await openAssessment()

    await screen.findByRole('columnheader', { name: M('assessment.col_evidence') }, { timeout: 5000 })
    // The servers view owns every control on this page; a verdict is evidence,
    // not a fifth thing to toggle.
    const panel = screen.getByRole('tabpanel')
    expect(within(panel).queryAllByRole('switch')).toHaveLength(0)
  })
  // The measurement control. The assessment is only worth as much as the number
  // of rows carrying a verdict, and the pass that produces them was previously
  // reachable only through an icon whose purpose lived in an aria-label.
  const unknown: Rec = {
    strength: 'unknown',
    recommendShare: false,
    reasons: [{ code: 'not_probed', detail: '' }],
  }
  const idle = { running: false, done: 0, total: 0 }

  it('names how many servers are unmeasured, in visible text', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idle as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        withRec({ name: 'alpha-mcp' }, unknown),
        withRec({ name: 'bravo-mcp' }, unknown),
        withRec({ name: 'charlie-mcp' }, noObjection),
      ],
    } as never)
    await openAssessment()
    expect(
      await screen.findByRole('button', { name: M('assessment.measure_unmeasured', { count: 2 }) }, { timeout: 5000 }),
    ).toBeTruthy()
  })

  it('counts a row carrying no verdict at all as unmeasured', async () => {
    // An older gateway reached through Make Live sends no verdict field; that row
    // is exactly as unmeasured as one whose verdict says so.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idle as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' })],
    } as never)
    await openAssessment()
    expect(
      await screen.findByRole('button', { name: M('assessment.measure_unmeasured', { count: 1 }) }, { timeout: 5000 }),
    ).toBeTruthy()
  })

  it('offers nothing to press when every server is measured', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idle as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, noObjection)],
    } as never)
    await openAssessment()
    const btn = await screen.findByRole('button', { name: M('assessment.measure_none_left') }, { timeout: 5000 })
    expect((btn as HTMLButtonElement).disabled).toBe(true)
  })

  it('starts the pass through the measure endpoint', async () => {
    // A probe pass measures a couple of servers and is not what this promises.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idle as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    const start = vi
      .spyOn(api, 'mcpMeasureStart')
      .mockResolvedValue({ running: true, done: 0, total: 1 } as never)
    await openAssessment()
    ;(await screen.findByRole('button', { name: M('assessment.measure_unmeasured', { count: 1 }) }, { timeout: 5000 })).click()
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1), { timeout: 5000 })
  })

  it('reports where a running pass got to', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(
      { running: true, done: 4, total: 9 } as never,
    )
    await openAssessment()
    expect(await screen.findByText(M('assessment.measure_running', { done: 4, total: 9 }), { timeout: 5000 })).toBeTruthy()
  })

  it('re-reads the servers when a pass finishes, so the table stops lying', async () => {
    // Without this the operator watches progress reach the end and then reads a
    // table still saying "not measured" beside a button still offering the same
    // count. That contradiction is the end of every single use of the control.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    const servers = vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    // Running first, then settled: the refresh must key on that edge.
    vi.spyOn(api, 'mcpMeasureProgress')
      .mockResolvedValueOnce({ running: true, done: 0, total: 1 } as never)
      .mockResolvedValue({ running: false, done: 1, total: 1 } as never)
    await openAssessment()
    await screen.findByText(M('assessment.measure_running', { done: 0, total: 1 }), { timeout: 5000 })
    const before = servers.mock.calls.length
    await waitFor(
      () => expect(servers.mock.calls.length).toBeGreaterThan(before),
      { timeout: 4000 },
    )
  })

  it('does not close with the attempted count when the pass stopped early', async () => {
    // A pass that dies at 1 of 5 rendered the failure line AND a closure line
    // reading "Measured 5 servers", so it reported both that it stopped and that
    // it did all five. The closure line counts what was MEASURED, and is withheld
    // entirely when the pass failed.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue(
      { running: true, done: 0, total: 5 } as never,
    )
    // Running first, then stopped early: the readout must not close on the total.
    vi.spyOn(api, 'mcpMeasureProgress')
      .mockResolvedValueOnce({ running: true, done: 0, total: 5 } as never)
      .mockResolvedValue(
        { running: false, done: 1, total: 5, error: 'RuntimeError' } as never,
      )
    await openAssessment()
    // The press is what makes a closure line eligible at all, so the bug needs it.
    ;(await screen.findByRole('button', { name: M('assessment.measure_unmeasured', { count: 1 }) }, { timeout: 5000 })).click()

    await waitFor(() => expect(screen.getByText(M('assessment.measure_failed'))).toBeTruthy(), {
      timeout: 4000,
    })
    // No closure line at all on a failed pass, whatever number it would carry:
    // "stopped early" and "Measured N server(s)" together is the contradiction.
    expect(screen.queryByText(startsWithLine('assessment.measure_finished'))).toBeNull()
  })

  it('says a pass stopped early rather than letting it read as finished', async () => {
    // A pass that died is otherwise indistinguishable from one that had nothing
    // to do, which is the difference between measured and never measured.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(
      { running: false, done: 1, total: 5, error: 'RuntimeError' } as never,
    )
    await openAssessment()
    expect(await screen.findByText(M('assessment.measure_failed'), undefined, { timeout: 5000 })).toBeTruthy()
  })

  it('closes with the number MEASURED, not the number attempted', async () => {
    // A pre-flight that could not run leaves no verdict on purpose, so a pass can
    // attempt five servers and measure two. Closing on the attempt count told the
    // operator five rows had been assessed while three still read "not measured"
    // in the table beside it and the button still offered them.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue(
      { running: true, done: 0, measured: 0, total: 5 } as never,
    )
    vi.spyOn(api, 'mcpMeasureProgress')
      // MeasureControl reads progress on MOUNT, before this test presses Start --
      // a pass already running there would disable the button and the press would
      // be a no-op. So the mount read reports an idle gateway; the pass only
      // starts on the press.
      .mockResolvedValueOnce({ running: false, done: 0, measured: 0, total: 0 } as never)
      // The press arms the poll (mcpMeasureStart reports running); the first poll
      // after it already reports the finished pass, so this test spends one poll
      // interval rather than two. What it pins is the CLOSING number, and the
      // intermediate running readout is pinned by its own test above.
      .mockResolvedValue(
        { running: false, done: 5, measured: 2, total: 5 } as never,
      )
    await openAssessment()
    ;(await screen.findByRole('button', { name: M('assessment.measure_unmeasured', { count: 1 }) }, { timeout: 5000 })).click()

    expect(await screen.findByText(M('assessment.measure_finished', { count: 2 }), undefined, { timeout: 4000 }))
      .toBeTruthy()
    // The number that was on screen before, and the reason this test exists.
    expect(screen.queryByText(M('assessment.measure_finished', { count: 5 }))).toBeNull()
  })

  it('says nothing rather than claiming a pass that reached nothing measured everything', async () => {
    // Every server unreachable is not a corner: the probe cannot spawn at all on
    // some hosts, so the whole configuration takes the "could not ask" branch. The
    // pass did not fail -- each measurement resolved to "unmeasurable" and no error
    // was raised -- so the closure line is the ONLY thing that could speak here,
    // and on the attempt count it said "Measured 3 servers" having measured none.
    // Withheld entirely: silent, but never false. The button's own unchanged count
    // is what tells the reader nothing landed.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, unknown)],
    } as never)
    vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue(
      { running: true, done: 0, measured: 0, total: 3 } as never,
    )
    vi.spyOn(api, 'mcpMeasureProgress')
      .mockResolvedValueOnce({ running: true, done: 0, measured: 0, total: 3 } as never)
      .mockResolvedValue(
        { running: false, done: 3, measured: 0, total: 3 } as never,
      )
    await openAssessment()
    ;(await screen.findByRole('button', { name: M('assessment.measure_unmeasured', { count: 1 }) }, { timeout: 5000 })).click()

    // Prove the pass reached the RUNNING state first. Waiting only for the absence
    // of the running line is satisfied before the pass starts as well as after it
    // ends, so the assertion below would run against a page that has not polled
    // yet and would pass against the bug too -- this test was vacuous until this
    // line was added, verified by a mutation it failed to kill.
    await screen.findByText(M('assessment.measure_running', { done: 0, total: 3 }), { timeout: 5000 })
    await waitFor(
      () => expect(screen.queryByText(startsWithLine('assessment.measure_running'))).toBeNull(),
      { timeout: 4000 },
    )
    expect(screen.queryByText(startsWithLine('assessment.measure_finished'))).toBeNull()
    // Not a failure either: nothing raised, so the error line must stay away.
    expect(screen.queryByText(M('assessment.measure_failed'))).toBeNull()
  })
})

describe('sharing assessment evidence cell', () => {
  it('drops the generic reason when the row has one of its own', async () => {
    // `no_objection_found` repeats the Assessment pill, so on a healthy fleet it
    // would appear on nearly every row and compete with the rows that carry a
    // real observation.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        {
          ...server({ name: 'alpha-mcp' }),
          recommendation: {
            strength: 'no_objection',
            recommendShare: false,
            reasons: [
              { code: 'no_objection_found', detail: '' },
              { code: 'all_tools_read_only', detail: '' },
            ],
          },
        },
      ],
    } as never)
    mount()
    fireEvent.mouseDown(await screen.findByRole('tab', { name: M('view_assessment') }, { timeout: 5000 }))

    expect(await screen.findByText(M('assessment.reason_all_tools_read_only'), undefined, { timeout: 5000 })).toBeTruthy()
    // The generic line is gone from the cell; the legend still carries the caveat.
    expect(screen.queryByText(M('assessment.reason_no_objection_found'))).toBeNull()
  })

  it('keeps the generic reason when it is the only one', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        {
          ...server({ name: 'alpha-mcp' }),
          recommendation: {
            strength: 'no_objection',
            recommendShare: false,
            reasons: [{ code: 'no_objection_found', detail: '' }],
          },
        },
      ],
    } as never)
    mount()
    fireEvent.mouseDown(await screen.findByRole('tab', { name: M('view_assessment') }, { timeout: 5000 }))

    // An empty Evidence cell beside a filled verdict would read as missing data.
    expect(await screen.findByText(M('assessment.reason_no_objection_found'), undefined, { timeout: 5000 })).toBeTruthy()
  })
})

describe('the warning reaches the decision point', () => {
  const refuted = {
    strength: 'refuted',
    recommendShare: false,
    reasons: [{ code: 'observed_hazard', detail: 'unroutable_notification' }],
  }

  it('marks the flagged row on the servers table the warning sends you to', async () => {
    // "Open Servers" is useless if the rows it counted are indistinguishable
    // there, so the same marker appears on both views.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp', 'beta-mcp'], stub_count: 2 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        { ...server({ name: 'alpha-mcp', stub: true, in_allowlist: true }), recommendation: refuted },
        {
          ...server({ name: 'beta-mcp', stub: true, in_allowlist: true }),
          recommendation: {
            strength: 'no_objection',
            recommendShare: false,
            reasons: [{ code: 'no_objection_found', detail: '' }],
          },
        },
      ],
    } as never)
    mount()

    // Both rows read "shared"; only the flagged one carries the marker, so the
    // count is exactly one rather than one-per-shared-row.
    const marked = (await screen.findAllByText(M('state_shared'), { selector: 'span' }))
      .filter(el => el.querySelector('svg') !== null)
    expect(marked).toHaveLength(1)
  })
})

// The bulk action, whose whole risk is claiming to have done more than it did.
describe('stub every server the evidence allows', () => {
  // What the engine actually emits for each tier: MEASURED recommends the stub
  // and withholds sharing (the pre-flight compares the handshake, not tool-call
  // state), DECLARED recommends both.
  const measured = {
    strength: 'measured',
    recommendStub: true,
    recommendShare: false,
    reasons: [{ code: 'preflight_passed', detail: '' }],
  }
  const declared = {
    strength: 'declared',
    recommendStub: true,
    recommendShare: true,
    reasons: [
      { code: 'declares_caller_identity', detail: '' },
      { code: 'preflight_passed', detail: '' },
    ],
  }
  const disqualified = {
    strength: 'disqualified',
    recommendStub: false,
    recommendShare: false,
    reasons: [{ code: 'session_bound_by_construction', detail: '' }],
  }
  const idleProgress = { running: false, done: 0, total: 0, error: '' }

  it('re-reads the rows after the wait instead of trusting the click', async () => {
    // The measurement pass changes which servers are candidates at all: a row
    // absent at click time can exist by the end, and it is shared in the same press.
    vi.useFakeTimers()
    try {
      vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
      vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue({ ...idleProgress, running: true } as never)
      vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
      const rows = vi
        .spyOn(api, 'mcpGatewayServers')
        .mockResolvedValueOnce({
          servers: [{ ...server({ name: 'fresh-mcp' }), recommendation: measured }],
        } as never)
        .mockResolvedValue({
          servers: [
            { ...server({ name: 'fresh-mcp' }), recommendation: measured },
            { ...server({ name: 'late-mcp' }), recommendation: declared },
          ],
        } as never)
      const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation((async (name: string) => ({
        ok: true,
        name,
        stub: true,
        stubbed: [name],
      })) as never)
      vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation((async (name: string) => preview(name)) as never)

      mount()
      await vi.waitFor(() => screen.getByText('fresh-mcp'), { timeout: 5000 })
      fireEvent.click(screen.getByRole('button', { name: M('enable_all') }))
      await vi.advanceTimersByTimeAsync(2 * 1000)

      // The first press lists the launches; the second shares them, each
      // pinned to the launch it listed, with eligibility decided per name.
      await vi.waitFor(() => screen.getByRole('button', { name: M('share_all_confirm', { count: 2 }) }), { timeout: 5000 })
      expect(setStub).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: M('share_all_confirm', { count: 2 }) }))
      // Chain: one stub POST per listed server, sent in turn.
      await vi.waitFor(
        () => expect(setStub).toHaveBeenCalledWith('late-mcp', true, 'late-mcp-cmd:late-mcp-env', true),
        { timeout: 5000 },
      )
      expect(rows.mock.calls.length).toBeGreaterThan(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('measures the unmeasured before sharing, then shares the fresh candidates', async () => {
    vi.useFakeTimers()
    try {
      vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
      const start = vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue({
        ...idleProgress,
        running: true,
      } as never)
      vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
      const servers = vi
        .spyOn(api, 'mcpGatewayServers')
        .mockResolvedValueOnce({ servers: [server({ name: 'fresh-mcp' })] } as never)
        .mockResolvedValue({
          servers: [{ ...server({ name: 'fresh-mcp' }), recommendation: declared }],
        } as never)
      const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
        ok: true, name: 'fresh-mcp', stub: true, stubbed: ['fresh-mcp'],
      } as never)
      vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation((async (name: string) => preview(name)) as never)

      mount()
      await vi.waitFor(() => screen.getByText('fresh-mcp'), { timeout: 5000 })
      fireEvent.click(screen.getByRole('button', { name: M('enable_all') }))
      await vi.advanceTimersByTimeAsync(2 * 1000)

      // Chain: servers re-read -> one batched launch read -> the confirm button.
      await vi.waitFor(() => screen.getByRole('button', { name: M('share_all_confirm', { count: 1 }) }), { timeout: 5000 })
      fireEvent.click(screen.getByRole('button', { name: M('share_all_confirm', { count: 1 }) }))
      await vi.waitFor(() => expect(setStub).toHaveBeenCalledWith('fresh-mcp', true, 'fresh-mcp-cmd:fresh-mcp-env', true), { timeout: 5000 })
      expect(start).toHaveBeenCalledTimes(1)
      expect(servers.mock.calls.length).toBeGreaterThan(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('counts what each write answered, not what the list asked for', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        { ...server({ name: 'good-mcp' }), recommendation: declared },
        { ...server({ name: 'failed-mcp' }), recommendation: declared },
        { ...server({ name: 'ineligible-mcp' }), recommendation: disqualified },
      ],
    } as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation((async (name: string) => {
      if (name === 'failed-mcp') throw new Error('write failed')
      if (name === 'ineligible-mcp') {
        return { ok: true, name, stub: true, stubbed: [], skipped: [{ name, reason: 'evidence_disqualified' }] }
      }
      return { ok: true, name, stub: true, stubbed: [name], skipped: [] }
    }) as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation((async (name: string) => preview(name)) as never)

    mount()
    await screen.findByText('good-mcp', undefined, { timeout: 5000 })
    fireEvent.click(await screen.findByRole('button', { name: M('enable_all') }, { timeout: 5000 }))
    // Chain: servers re-read -> one batched launch read -> the confirm button.
    fireEvent.click(await screen.findByRole('button', { name: M('share_all_confirm', { count: 3 }) }, { timeout: 5000 }))

    // Chain: one stub POST per listed server -> the result notice.
    await waitFor(() => expect(screen.getByText(M('enable_all_review_result_shared', { count: 1 }), { exact: false })).toBeTruthy(), { timeout: 5000 })
    // The skipped server is named, not just counted.
    expect(screen.getByText(M('enable_all_review_skipped_names', { names: 'ineligible-mcp' }), { exact: false })).toBeTruthy()
    expect(screen.getByText(M('enable_all_review_failed_names', { names: 'failed-mcp' }), { exact: false })).toBeTruthy()
  })

  it('reads every candidate launch in one request', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        { ...server({ name: 'a-mcp' }), recommendation: declared },
        { ...server({ name: 'b-mcp' }), recommendation: declared },
      ],
    } as never)
    const single = vi.spyOn(api, 'mcpGatewayLaunchPreview')
    const batch = vi.spyOn(api, 'mcpGatewayLaunchPreviews').mockResolvedValue({
      launches: { 'a-mcp': preview('a-mcp'), 'b-mcp': preview('b-mcp') },
      errors: {},
    } as never)

    mount()
    await screen.findByText('a-mcp', undefined, { timeout: 5000 })
    fireEvent.click(await screen.findByRole('button', { name: M('enable_all') }, { timeout: 5000 }))
    await screen.findByRole('button', { name: M('share_all_confirm', { count: 2 }) }, { timeout: 5000 })

    expect(batch).toHaveBeenCalledTimes(1)
    expect(batch).toHaveBeenCalledWith(['a-mcp', 'b-mcp'])
    expect(single).not.toHaveBeenCalled()
  })

  it('says so when sharing was turned on but did not come up', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['good-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [{ ...server({ name: 'good-mcp', stub: true, in_allowlist: true }), recommendation: declared }],
    } as never)
    const enable = vi
      .spyOn(api, 'mcpGatewayEnable')
      .mockResolvedValue({ ok: true, enabled: true, running: false, ping_ok: false } as never)

    mount()
    const sharing = await screen.findByRole('switch', { name: M('hero_title') }, { timeout: 5000 })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(sharing)
    fireEvent.click(await screen.findByRole('button', { name: M('confirm_turn_on') }, { timeout: 5000 }))

    await waitFor(() => expect(enable).toHaveBeenCalledWith(true), { timeout: 5000 })
    expect(await screen.findByText(M('sharing_not_live'), { exact: false }, { timeout: 5000 })).toBeTruthy()
  })

  it('shows the live pass position instead of a counter frozen at zero', async () => {
    // The wait runs for up to four minutes. A hardcoded 0 read as a stalled pass
    // on exactly the fresh install this control serves, so the poll's readings
    // have to reach the line beside the button.
    const { fireEvent } = await import('@testing-library/react')
    vi.useFakeTimers()
    try {
      vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
      vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue({ ...idleProgress, running: true } as never)
      vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue({
        running: true,
        done: 3,
        total: 7,
        error: '',
      } as never)
      vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
        servers: [server({ name: 'fresh-mcp' })],
      } as never)

      mount()
      await vi.waitFor(() => screen.getByText('fresh-mcp'), { timeout: 5000 })
      fireEvent.click(screen.getByRole('button', { name: M('enable_all') }))
      await vi.advanceTimersByTimeAsync(2 * 1000)

      await vi.waitFor(() => screen.getByText(M('assessment.measure_running', { done: 3, total: 7 })), { timeout: 5000 })
    } finally {
      vi.useRealTimers()
    }
  })

  it('changes nothing while a pass it started is still running', async () => {
    const { fireEvent } = await import('@testing-library/react')
    vi.useFakeTimers()
    try {
      vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
      vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue({ ...idleProgress, running: true } as never)
      // Never stops: the wait has to give up rather than act on a half-measured
      // fleet, which would stub whatever happened to be done by then.
      vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue({
        running: true,
        done: 1,
        total: 9,
        error: '',
      } as never)
      vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
        servers: [server({ name: 'fresh-mcp' })],
      } as never)
      const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

      mount()
      await vi.waitFor(() => screen.getByText('fresh-mcp'), { timeout: 5000 })
      fireEvent.click(screen.getByRole('button', { name: M('enable_all') }))
      // Past the wait deadline.
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000)

      expect(setStub).not.toHaveBeenCalled()
      await vi.waitFor(() => screen.getByText(M('enable_all_still_measuring')), { timeout: 5000 })
    } finally {
      vi.useRealTimers()
    }
  })

  it('renders the measured tier with its own label, not "not measured"', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [{ ...server({ name: 'good-mcp' }), recommendation: measured }],
    } as never)
    mount()
    fireEvent.mouseDown(await screen.findByRole('tab', { name: M('view_assessment') }, { timeout: 5000 }))
    // An unmapped tier falls back to "not measured", which would read as the
    // measurement having never happened.
    expect(await screen.findByText(M('assessment.strength_measured'), { timeout: 5000 })).toBeTruthy()
  })

})

// The bug this branch fixes: the STATE chip was derived from allowlist
// membership and the global switch, never from what the rewriter actually did.
// A server the rewriter LEFT UNWRAPPED (it declares an env key, so it launches
// per session with no pooled backend) still read as "shared" with its toggle
// lit. The backend already sends `pooling_blocked_by_env`; the chip now honours
// it. Absence of the field must keep reading as today, for the older-gateway
// contract the field's own comment states.
describe('state chip honours the rewriter, not just the allowlist', () => {
  it('reads a blocked, allowlisted, sharing-on server as unwrapped, not shared', async () => {
    // Every input the OLD mapping consulted says "shared": in the allowlist,
    // stub true, global sharing on. Only the rewriter's own verdict says
    // otherwise, and it is the one that decides.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({
          name: 'alpha-mcp',
          stub: true,
          in_allowlist: true,
          pooling_blocked_by_env: true,
        }),
      ],
    } as never)

    mount()
    // The chip no longer claims a shared backend the broker never built.
    const chip = await screen.findByText(M('state_direct'), { selector: 'span' }, { timeout: 5000 })
    expect(chip).toBeTruthy()
    // A state is a term, not a sentence. Every shipped locale renders this longer
    // than the English, and a badge broken across two ragged lines beside the
    // single-line `shared` and `direct` pills reads as unfinished UI.
    expect(chip.className).toContain('whitespace-nowrap')
    expect(screen.queryByText(M('state_shared'), { selector: 'span' })).toBeNull()
  })

  it('does not colour a blocked row as if it were sharing', async () => {
    // The defect this PR exists to remove was the page contradicting itself, and
    // a chip whose TEXT says unwrapped while its COLOUR is the shared accent
    // reproduces it on one row: an operator scanning the column by colour reads
    // the old answer. Styling and label are therefore driven by one derivation.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({
          name: 'alpha-mcp',
          stub: true,
          in_allowlist: true,
          pooling_blocked_by_env: true,
        }),
      ],
    } as never)

    mount()
    const chip = await screen.findByText(M('state_direct'), { selector: 'span' }, { timeout: 5000 })
    expect(chip.className).not.toContain('--accent')
    expect(chip.className).toContain('--muted')
  })

  it('still colours a genuinely shared row with the accent', async () => {
    // The counterpart the negative test above cannot give: withholding the accent
    // from a blocked row is only correct if the accent still arrives for a row that
    // IS pooled. Both now read one derivation -- `stateLabelKey` -- so this fails
    // the moment the colour is keyed off a second spelling of "is shared" that
    // disagrees with the label.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({
          name: 'alpha-mcp',
          stub: true,
          in_allowlist: true,
          pooling_blocked_by_env: false,
        }),
      ],
    } as never)

    mount()
    const chip = await screen.findByText(M('state_shared'), { selector: 'span' }, { timeout: 5000 })
    expect(chip.className).toContain('--accent')
    expect(screen.queryByText(M('state_direct_env_reason'))).toBeNull()
  })

  it('does not flag a blocked row as sharing without support', async () => {
    // A row the rewriter left unwrapped is not sharing at all, so contrary
    // evidence about sharing cannot apply to it. The same predicate feeds the
    // assessment view's count, so excluding it here keeps that number honest too:
    // it must not offer to send the operator to a row that is not co-tenanted.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({
          name: 'alpha-mcp',
          stub: true,
          in_allowlist: true,
          pooling_blocked_by_env: true,
          recommendation: {
            strength: 'refuted',
            recommendStub: false,
            recommendShare: false,
            reasons: [{ code: 'observed_hazard', detail: 'unroutable_notification' }],
          },
        }),
      ],
    } as never)

    mount()
    const chip = await screen.findByText(M('state_direct'), { selector: 'span' }, { timeout: 5000 })
    // `refuted` on a row that IS sharing draws the danger style; here it must not.
    expect(chip.className).not.toContain('--danger')
  })

  it('leaves an un-stubbed blocked row reading direct, with no reason line', async () => {
    // Two different states must not collapse into one label. On an un-stubbed row
    // the field is forward-looking -- "stubbing this would still not pool it",
    // which is what the batch action uses it for -- while the row's CURRENT state
    // is plainly `direct`. Reporting it as the blocked state would lose the fact
    // that the stub is not in the path (the page's own lede says the stub is what
    // enables server-authored UI) and would offer advice that changes nothing
    // until the server is stubbed.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({
          name: 'echo-mcp',
          stub: false,
          in_allowlist: false,
          pooling_blocked_by_env: true,
        }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(M('state_direct'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(M('state_direct_env_reason'))).toBeNull()
  })

  it('keeps the label off the global switch, as defensive rendering', async () => {
    // Scope stated honestly: the backend computes the field as
    // `gw_cfg.enabled and <withheld env>`, so with sharing OFF it is always false
    // and a real gateway never sends this combination. What this pins is that the
    // CLIENT does not re-introduce the dependency -- the mapping reads the
    // rewriter's verdict alone, so a later backend change cannot silently restore
    // the old behaviour here.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'alpha-mcp', stub: true, in_allowlist: true, pooling_blocked_by_env: true }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(M('state_direct'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    // Both states read "direct" now; the env reason in Details tells them apart.
    expect(screen.queryByText(M('state_direct_env_reason'))).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: M('details_aria', { name: 'alpha-mcp' }) }))
    expect(await screen.findByText(M('state_direct_env_reason'), undefined, { timeout: 5000 })).toBeTruthy()
  })

  it('renders exactly as today when the field is absent (older-gateway contract)', async () => {
    // The regression guard for the field's own contract: a dashboard pointed at
    // an older gateway receives no `pooling_blocked_by_env`, and its ABSENCE must
    // read as "no obstacle known", never as an obstacle. A stubbed, allowlisted,
    // sharing-on server still reads "shared" with no reason line.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ name: 'alpha-mcp', stub: true, in_allowlist: true })],
    } as never)

    mount()
    expect(await screen.findByText(M('state_shared'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(M('state_direct'), { selector: 'span' })).toBeNull()
    expect(screen.queryByText(M('state_direct_env_reason'))).toBeNull()
  })

  it('tells a marked env-blocked row apart from an unmarked one on the assessment tab', async () => {
    // Both read "One per chat"; only the reason line separates a server the
    // operator marked (kept unshared by its env) from one never marked, and the
    // audit view is where that difference has to be readable.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'alpha-mcp', stub: true, in_allowlist: true, pooling_blocked_by_env: true }),
        server({ name: 'beta-mcp', stub: false, in_allowlist: false }),
      ],
    } as never)

    mount()
    fireEvent.mouseDown(await screen.findByRole('tab', { name: M('view_assessment') }, { timeout: 5000 }))
    const panel = await screen.findByRole('tabpanel', undefined, { timeout: 5000 })
    const rows = await within(panel).findAllByRole('row')
    const alpha = rows.find(r => r.textContent?.includes('alpha-mcp'))!
    const beta = rows.find(r => r.textContent?.includes('beta-mcp'))!
    expect(within(alpha).getByText(M('state_direct'), { selector: 'span' })).toBeTruthy()
    expect(within(beta).getByText(M('state_direct'), { selector: 'span' })).toBeTruthy()
    expect(alpha.textContent).toContain(M('state_direct_env_reason'))
    expect(beta.textContent).not.toContain(M('state_direct_env_reason'))
  })

  it('reads pooling_blocked_by_env:false as no obstacle, exactly like absent', async () => {
    // Only `=== true` is an obstacle. An explicit false is the gateway saying it
    // measured no block, and must behave identically to the field being absent.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'alpha-mcp', stub: true, in_allowlist: true, pooling_blocked_by_env: false }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(M('state_shared'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(M('state_direct'), { selector: 'span' })).toBeNull()
  })

  it('reads a private backend as stub, not shared, with sharing on', async () => {
    // A server whose env names a vault secret is wrapped onto a backend of its
    // own. The global switch being on does not make it shared.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({
          name: 'alpha-mcp',
          stub: true,
          in_allowlist: true,
          pooling_blocked_by_env: false,
          private_backend: true,
        }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(M('state_stub'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(M('state_shared'), { selector: 'span' })).toBeNull()
  })

  it('keeps the existing states unchanged for unblocked rows', async () => {
    // direct: not stubbed, not blocked. no stdio: can_stub false. These are the
    // paths the fix must not disturb.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'direct-mcp' }),
        server({ name: 'nostdio-mcp', can_stub: false }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(M('state_direct'), { selector: 'span' }, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(M('state_no_stub'), { selector: 'span' })).toBeTruthy()
  })

  it('counts the switches that read on, not the rows still waiting', async () => {
    // The header and the STUB column have to agree. A row waiting for re-approval
    // keeps its opt-in in config while its switch reads off, so a header counting the
    // config claims two opted in above a column showing one switch on, and leaves the
    // operator to work out which of the two numbers is true.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp', 'beta-mcp'],
        stub_count: 2,
        running: true,
        ping_ok: true,
        launch_refused: {
          'beta-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['npx', '-y', 'beta-mcp@2']],
            envs: [[]],
            expected_launch: 'b:b',
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'alpha-mcp', stub: true, in_allowlist: true }),
        server({ name: 'beta-mcp', stub: true, in_allowlist: true }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(M('stubbed_of_total', { stubbed: 1, total: 2 }), undefined, { timeout: 5000 })).toBeTruthy()
    // The claim the counter has to agree with: one shared, one waiting to confirm.
    expect(screen.getByRole('switch', { name: `${M('share_toggle')} alpha-mcp` })).toBeTruthy()
    expect(
      screen.getByRole('button', { name: `${M('new_command_button')} beta-mcp` }),
    ).toBeTruthy()
  })

  it('gaps the servers panel cards the same way the assessment view does', async () => {
    // The lede header, the two inline banners and the three cards are SIBLINGS
    // inside the servers tab panel. The `space-y-4` on the <Tabs> root only gaps
    // the tab rail from the panel; it cannot reach inside the panel, so without a
    // gap class ON the panel the cards render flush against each other -- the
    // inconsistency this fix removes. Assert the panel carries the spacing class
    // so a later refactor that drops it fails here rather than only on screen.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    // The active tab panel is the servers view (the default). Radix renders it
    // with role="tabpanel"; the inactive assessment panel is not in the DOM.
    const panel = await screen.findByRole('tabpanel', undefined, { timeout: 5000 })
    expect(panel.className).toContain('space-y-4')
  })
})
