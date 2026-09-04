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
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { McpManagement } from '../pages/settings/McpManagement'
import { api } from '../api/client'

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
        <McpManagement />
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
})
afterEach(cleanup)

describe('McpManagement', () => {
  it('reports a stub that saved but did not come up', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    // The broker failed to start: the endpoint still answers 200.
    vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      name: 'alpha-mcp',
      stub: true,
      applied: false,
    } as never)

    mount()
    const row = await screen.findByRole('switch', { name: /alpha-mcp/i })
    row.click()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
    })
  })

  it('asks for a restart without painting it as a failure', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    // The normal outcome: the allowlist is stored and nothing was cycled, so the
    // change is pending a restart rather than broken.
    vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      name: 'alpha-mcp',
      stub: true,
      applied: false,
      restart_required: true,
    } as never)

    mount()
    const row = await screen.findByRole('switch', { name: /alpha-mcp/i })
    row.click()

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
      expect(screen.queryByText(/no mcp servers are configured/i)).toBeNull()
      expect(screen.getByText(/could not load the server list/i)).toBeTruthy()
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
    const row = await screen.findByRole('switch', { name: /alpha-mcp/i })
    // ON and unsupported: turning it OFF must stay reachable.
    await waitFor(() => expect((row as HTMLButtonElement).disabled).toBe(false))

    const sharing = screen.getByRole('switch', { name: /share backends/i })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false))
  })

  it('blocks enabling a stub on an unsupported platform', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ supported: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    const row = await screen.findByRole('switch', { name: /alpha-mcp/i })
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
    const sharing = await screen.findByRole('switch', { name: /share backends/i })
    await waitFor(() => expect((sharing as HTMLButtonElement).disabled).toBe(false))
    const before = statusSpy.mock.calls.length
    sharing.click()

    // The confirm dialog guards enabling; take it.
    const confirm = await screen.findByRole('button', { name: /turn on sharing/i })
    confirm.click()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(statusSpy.mock.calls.length).toBeGreaterThan(before)
    })
    expect(screen.getByRole('alert').textContent ?? '').not.toMatch(/nothing was saved/i)
  })

  it('refuses to arm sharing while nothing is stubbed, but allows disarming it', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    const { unmount } = mount()
    const off = await screen.findByRole('switch', { name: /share backends/i })
    await waitFor(() => expect((off as HTMLButtonElement).disabled).toBe(true))
    unmount()
    cleanup()

    // Already on with nothing stubbed — the state this PR removes. It must still
    // be escapable, or the operator is stuck with a switch they cannot clear.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    mount()
    const on = await screen.findByRole('switch', { name: /share backends/i })
    // Poll: the switch mounts before the status query resolves, and until it does
    // the page cannot know the setting is already on.
    await waitFor(() => expect((on as HTMLButtonElement).disabled).toBe(false))
  })

  it('explains why sharing is disabled instead of just refusing the click', async () => {
    // A disabled headline control that gives no reason reads as a broken page:
    // the first click a new user makes does nothing and says nothing.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    expect(await screen.findByText(/stub at least one server below/i)).toBeTruthy()
  })

  it('names the sharing-on-with-nothing-stubbed state rather than showing a live switch', async () => {
    // Reachable by unstubbing the last server: the switch stays on over an empty
    // set, which is the "switch with no observable effect" state this page exists
    // to eliminate.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    expect(await screen.findByText(/sharing is on, but nothing is stubbed/i)).toBeTruthy()
    // and the reason for a DISABLED switch must not also be showing
    expect(screen.queryByText(/stub at least one server below/i)).toBeNull()
  })

  it('confirms sharing in a real modal dialog that Escape can dismiss', async () => {
    // The hand-rolled <div role="dialog"> it replaces had no focus trap, no
    // Escape handler and no focus return, so a keyboard user could Tab into the
    // page behind the overlay and had no way out.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)

    mount()
    const sw = await screen.findByRole('switch', { name: /share backends/i })
    await waitFor(() => expect((sw as HTMLButtonElement).disabled).toBe(false))
    sw.click()

    const dlg = await screen.findByRole('dialog')
    // Radix labels the dialog from DialogTitle; an unlabelled dialog is
    // announced as nothing by a screen reader. (This Radix version relies on
    // focus guards + scroll lock rather than setting `aria-modal`, so assert the
    // label and the behaviour, not that attribute.)
    expect(dlg.getAttribute('aria-labelledby')).toBeTruthy()
    // Initial focus must land INSIDE the dialog — the hand-rolled version left it
    // on the switch behind the overlay.
    await waitFor(() => expect(dlg.contains(document.activeElement)).toBe(true))

    const { fireEvent } = await import('@testing-library/react')
    fireEvent.keyDown(dlg, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('discloses that both switches are next-chat scoped', async () => {
    // The apply path rebuilds the provider factory and drains the warm pool but
    // deliberately leaves live sessions alone, so a toggle is NOT retroactive.
    // Without this line the row switch reads as broken: the operator flips it,
    // the page says applied, and their open chat still behaves the old way.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    expect(await screen.findByText(/changes here apply to new chats/i)).toBeTruthy()
  })

  it('does not claim that active sessions restart and pick up the change', async () => {
    // Regression guard on copy: the confirm dialog used to assert "Active
    // sessions restart once so they pick up the change", which is the OPPOSITE
    // of what refresh_defaults() does.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)

    mount()
    const sw = await screen.findByRole('switch', { name: /share backends/i })
    await waitFor(() => expect((sw as HTMLButtonElement).disabled).toBe(false))
    sw.click()
    const dlg = await screen.findByRole('dialog')

    expect(dlg.textContent || '').not.toMatch(/restart once so they pick up/i)
    expect(dlg.textContent || '').toMatch(/keep the setup they started with/i)
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
    const tab = await screen.findByRole('tab', { name: /sharing assessment/i })
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

    expect(await screen.findByText(/unsuitable for sharing/i)).toBeTruthy()
    // The translated reason AND the server's own verbatim detail, which is data
    // and must never be translated or dropped.
    expect(
      screen.getByText(/a shared backend never receives this credential/i),
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
    expect(await screen.findByText('not measured', { selector: 'span' })).toBeTruthy()
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
    expect(await screen.findByText('no objection found', { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText(/built for sharing/i)).toBeNull()
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

    expect(await screen.findByRole('status')).toBeTruthy()
    expect(screen.getByText(/already in effect/i)).toBeTruthy()
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

    await screen.findByText('no objection found', { selector: 'span' })
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

    await screen.findByText('not measured', { selector: 'span' })
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

    await screen.findByText('not measured', { selector: 'span' })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('carries no switches, because it decides nothing', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, noObjection)],
    } as never)
    await openAssessment()

    await screen.findByRole('columnheader', { name: /evidence/i })
    // The servers view owns every control on this page; a verdict is evidence,
    // not a fifth thing to toggle.
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
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
      await screen.findByRole('button', { name: /measure 2 servers/i }),
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
      await screen.findByRole('button', { name: /measure 1 server$/i }),
    ).toBeTruthy()
  })

  it('offers nothing to press when every server is measured', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idle as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [withRec({ name: 'alpha-mcp' }, noObjection)],
    } as never)
    await openAssessment()
    const btn = await screen.findByRole('button', { name: /every server has been measured/i })
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
    ;(await screen.findByRole('button', { name: /measure 1 server$/i })).click()
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
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
    expect(await screen.findByText(/measuring, 4 of 9 done/i)).toBeTruthy()
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
    await screen.findByText(/measuring, 0 of 1 done/i)
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
    ;(await screen.findByRole('button', { name: /measure 1 server$/i })).click()

    await waitFor(() => expect(screen.getByText(/stopped early/i)).toBeTruthy(), {
      timeout: 4000,
    })
    // No closure line at all on a failed pass, whatever number it would carry:
    // "stopped early" and "Measured N server(s)" together is the contradiction.
    expect(screen.queryByText(/^measured \d+ server/i)).toBeNull()
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
    expect(await screen.findByText(/stopped early/i)).toBeTruthy()
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
    ;(await screen.findByRole('button', { name: /measure 1 server$/i })).click()

    expect(await screen.findByText(/^measured 2 servers$/i, undefined, { timeout: 4000 }))
      .toBeTruthy()
    // The number that was on screen before, and the reason this test exists.
    expect(screen.queryByText(/^measured 5 servers$/i)).toBeNull()
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
    ;(await screen.findByRole('button', { name: /measure 1 server$/i })).click()

    // Prove the pass reached the RUNNING state first. Waiting only for the absence
    // of the running line is satisfied before the pass starts as well as after it
    // ends, so the assertion below would run against a page that has not polled
    // yet and would pass against the bug too -- this test was vacuous until this
    // line was added, verified by a mutation it failed to kill.
    await screen.findByText(/measuring, 0 of 3 done/i)
    await waitFor(
      () => expect(screen.queryByText(/measuring,/i)).toBeNull(),
      { timeout: 4000 },
    )
    expect(screen.queryByText(/^measured \d+ server/i)).toBeNull()
    // Not a failure either: nothing raised, so the error line must stay away.
    expect(screen.queryByText(/stopped early/i)).toBeNull()
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
    fireEvent.mouseDown(await screen.findByRole('tab', { name: /sharing assessment/i }))

    expect(await screen.findByText(/every tool declares itself read-only/i)).toBeTruthy()
    // The generic line is gone from the cell; the legend still carries the caveat.
    expect(screen.queryByText(/which is weaker than evidence that sharing is safe/i)).toBeNull()
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
    fireEvent.mouseDown(await screen.findByRole('tab', { name: /sharing assessment/i }))

    // An empty Evidence cell beside a filled verdict would read as missing data.
    expect(await screen.findByText(/nothing disqualifying was found/i)).toBeTruthy()
  })
})

describe('the warning reaches the decision point', () => {
  const refuted = {
    strength: 'refuted',
    recommendShare: false,
    reasons: [{ code: 'observed_hazard', detail: 'unroutable_notification' }],
  }

  it('counts contrary verdicts in the confirm dialog before sharing is on', async () => {
    // Sharing is OFF here, so the assessment banner is silent by design. The
    // operator about to turn it on is exactly who needs the number.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        { ...server({ name: 'alpha-mcp', stub: true, in_allowlist: true }), recommendation: refuted },
      ],
    } as never)
    mount()
    const sw = await screen.findByRole('switch', { name: /share backends/i })
    await waitFor(() => expect((sw as HTMLButtonElement).disabled).toBe(false))
    sw.click()

    const dlg = await screen.findByRole('dialog')
    expect(dlg.textContent || '').toMatch(/argues against sharing/i)
  })

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
    const marked = (await screen.findAllByText('shared', { selector: 'span' }))
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

  it('offers every stubbable row as a candidate and leaves the rest out', async () => {
    // What the client still owns: WHICH rows are worth asking about. Eligibility
    // itself is the server's, so a row is a candidate whenever it has a stdio pipe
    // and is not already stubbed -- verdict strength is deliberately not consulted
    // here, because a second copy of that rule could disagree with the one the
    // write uses.
    const { fireEvent } = await import('@testing-library/react')
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    const start = vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        { ...server({ name: 'good-mcp' }), recommendation: declared },
        { ...server({ name: 'measured-mcp' }), recommendation: measured },
        { ...server({ name: 'bad-mcp' }), recommendation: disqualified },
        // Already stubbed: nothing to ask for.
        { ...server({ name: 'done-mcp', stub: true, in_allowlist: true }), recommendation: declared },
        // No stdio pipe to interpose on, so it can never be a candidate.
        { ...server({ name: 'http-mcp', can_stub: false }), recommendation: declared },
      ],
    } as never)
    const many = vi.spyOn(api, 'mcpGatewaySetStubMany').mockResolvedValue({
      ok: true,
      names: ['good-mcp', 'measured-mcp', 'bad-mcp'],
      stub: true,
      stubbed: ['good-mcp'],
      skipped: [
        { name: 'measured-mcp', reason: 'evidence_insufficient' },
        { name: 'bad-mcp', reason: 'evidence_insufficient' },
      ],
      applied: true,
    } as never)

    mount()
    // Wait for the rows, not just the button: the control is correctly disabled
    // until rows are known, so clicking at first paint hits a dead button.
    await screen.findByText('good-mcp')
    const btn = await screen.findByRole('button', { name: /evidence allows/i })
    fireEvent.click(btn)

    await waitFor(() => expect(many).toHaveBeenCalled())
    // One request for the whole set — a per-row loop could land the allowlist
    // half-flipped.
    expect(many).toHaveBeenCalledTimes(1)
    expect(many).toHaveBeenCalledWith(['good-mcp', 'measured-mcp', 'bad-mcp'], true, true)
    // Nothing was unmeasured, so no spawns were paid for.
    expect(start).not.toHaveBeenCalled()
    // Counts come off the response: one written, two declined by the server.
    await waitFor(() => expect(screen.getByText(/Stubbed 1\./)).toBeTruthy())
    expect(screen.getByText(/Left 2 alone\./)).toBeTruthy()
  })

  it('re-reads the rows after the wait instead of trusting the click', async () => {
    // The measurement pass runs for minutes, and it changes which servers are
    // candidates at all -- a row absent at click time can exist by the end. The
    // sharing switch is deliberately NOT re-read here: that state belongs to the
    // server's decision now, taken inside the lock hold that writes.
    const { fireEvent } = await import('@testing-library/react')
    vi.useFakeTimers()
    try {
      vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
      vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue({ ...idleProgress, running: true } as never)
      vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
      // First paint shows one candidate; a second appears while the pass runs.
      // Sending the click-time list would miss it.
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
      const many = vi.spyOn(api, 'mcpGatewaySetStubMany').mockResolvedValue({
        ok: true,
        names: ['fresh-mcp', 'late-mcp'],
        stub: true,
        stubbed: ['late-mcp'],
        skipped: [{ name: 'fresh-mcp', reason: 'evidence_insufficient' }],
        applied: true,
      } as never)

      mount()
      await vi.waitFor(() => screen.getByText('fresh-mcp'))
      fireEvent.click(screen.getByRole('button', { name: /evidence allows/i }))
      await vi.advanceTimersByTimeAsync(2 * 1000)

      // The batch carries the row that only became visible after the wait.
      await vi.waitFor(() =>
        expect(many).toHaveBeenCalledWith(['fresh-mcp', 'late-mcp'], true, true),
      )
      // Read more than once: the render's copy plus the post-wait re-read.
      expect(rows.mock.calls.length).toBeGreaterThan(1)
      await vi.waitFor(() => screen.getByText(/Stubbed 1\./))
    } finally {
      vi.useRealTimers()
    }
  })

  it('measures the unmeasured before deciding, then acts on the fresh verdicts', async () => {
    const { fireEvent } = await import('@testing-library/react')
    vi.useFakeTimers()
    try {
      vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
      const start = vi.spyOn(api, 'mcpMeasureStart').mockResolvedValue({
        ...idleProgress,
        running: true,
      } as never)
      vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
      // First render has no verdict; the re-read after the pass has one. Acting
      // on the first would skip the very server the operator just installed.
      const servers = vi
        .spyOn(api, 'mcpGatewayServers')
        .mockResolvedValueOnce({ servers: [server({ name: 'fresh-mcp' })] } as never)
        .mockResolvedValue({
          servers: [{ ...server({ name: 'fresh-mcp' }), recommendation: declared }],
        } as never)
      const many = vi
        .spyOn(api, 'mcpGatewaySetStubMany')
        .mockResolvedValue({ ok: true, names: ['fresh-mcp'], stub: true, applied: true } as never)

      mount()
      // The unmeasured row is what enables the button here — the eligible set is
      // empty until the pass runs, which is the whole point of this case.
      await vi.waitFor(() => screen.getByText('fresh-mcp'))
      fireEvent.click(screen.getByRole('button', { name: /evidence allows/i }))
      // Past the first progress poll, which reports the pass already finished.
      await vi.advanceTimersByTimeAsync(2 * 1000)

      await vi.waitFor(() => expect(many).toHaveBeenCalledWith(['fresh-mcp'], true, true))
      expect(start).toHaveBeenCalledTimes(1)
      expect(servers.mock.calls.length).toBeGreaterThan(1)
    } finally {
      vi.useRealTimers()
    }
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
      await vi.waitFor(() => screen.getByText('fresh-mcp'))
      fireEvent.click(screen.getByRole('button', { name: /evidence allows/i }))
      await vi.advanceTimersByTimeAsync(2 * 1000)

      await vi.waitFor(() => screen.getByText(/3 of 7/))
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
      const many = vi.spyOn(api, 'mcpGatewaySetStubMany')

      mount()
      await vi.waitFor(() => screen.getByText('fresh-mcp'))
      fireEvent.click(screen.getByRole('button', { name: /evidence allows/i }))
      // Past the wait deadline.
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000)

      expect(many).not.toHaveBeenCalled()
      await vi.waitFor(() => screen.getByText(/Still measuring/i))
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports what the server decided, not what the request asked for', async () => {
    // The client sends candidates and the server resolves eligibility inside the
    // lock hold that writes them, so the two lists differ by design. Counting the
    // request would claim stubs that were skipped.
    const { fireEvent } = await import('@testing-library/react')
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        { ...server({ name: 'good-mcp' }), recommendation: declared },
        { ...server({ name: 'other-mcp' }), recommendation: declared },
      ],
    } as never)
    const many = vi.spyOn(api, 'mcpGatewaySetStubMany').mockResolvedValue({
      ok: true,
      names: ['good-mcp', 'other-mcp'],
      stub: true,
      stubbed: ['good-mcp'],
      skipped: [{ name: 'other-mcp', reason: 'evidence_insufficient' }],
      applied: true,
    } as never)

    mount()
    await screen.findByText('good-mcp')
    fireEvent.click(await screen.findByRole('button', { name: /evidence allows/i }))

    // Every stubbable row is offered as a candidate; the server does the filtering.
    await waitFor(() =>
      expect(many).toHaveBeenCalledWith(['good-mcp', 'other-mcp'], true, true),
    )
    // One stubbed, one skipped -- both read off the RESPONSE.
    await waitFor(() => expect(screen.getByText(/Stubbed 1\./)).toBeTruthy())
    expect(screen.getByText(/Left 1 alone\./)).toBeTruthy()
  })

  it('does not claim the gateway failed when the server skipped every candidate', async () => {
    // Nothing qualified, so the handler deliberately never calls the apply hook
    // and answers `applied: false` with no `restart_required`. Reading that as
    // "the gateway could not start" blames a failure for a write that was never
    // attempted -- the "0 of N" notice is the whole story.
    const { fireEvent } = await import('@testing-library/react')
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [{ ...server({ name: 'other-mcp' }), recommendation: declared }],
    } as never)
    vi.spyOn(api, 'mcpGatewaySetStubMany').mockResolvedValue({
      ok: true,
      names: ['other-mcp'],
      stub: true,
      stubbed: [],
      skipped: [{ name: 'other-mcp', reason: 'evidence_insufficient' }],
      applied: false,
    } as never)

    mount()
    await screen.findByText('other-mcp')
    fireEvent.click(await screen.findByRole('button', { name: /evidence allows/i }))

    await waitFor(() => expect(screen.getByText(/Left 1 alone\./)).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('renders the measured tier with its own label, not "not measured"', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [{ ...server({ name: 'good-mcp' }), recommendation: measured }],
    } as never)
    mount()
    fireEvent.mouseDown(await screen.findByRole('tab', { name: /sharing assessment/i }))
    // An unmapped tier falls back to "not measured", which would read as the
    // measurement having never happened.
    expect(await screen.findByText(/measured, no divergence/i)).toBeTruthy()
  })

  it('defines direct (env) on the assessment tab too, not only under the servers table', async () => {
    // The assessment view renders the same chips through the same derivation, but
    // its footer is a DIFFERENT catalog key. A term defined only under the servers
    // tab leaves the operator auditing sharing here -- this fix's own audience --
    // reading an unexpanded `(env)` with no definition in frame and no reason line,
    // since that line is a servers-table row affordance.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({ enabled: true, stub: ['alpha-mcp'], stub_count: 1 }) as never,
    )
    vi.spyOn(api, 'mcpMeasureProgress').mockResolvedValue(idleProgress as never)
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
    fireEvent.mouseDown(await screen.findByRole('tab', { name: /sharing assessment/i }))
    expect(await screen.findByText('direct (env)', { selector: 'span' })).toBeTruthy()
    const def = await screen.findByText(
      /direct \(env\) — the server declares an environment key/i,
    )
    expect(def.textContent).toMatch(/does not stub it at all/i)
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
    const chip = await screen.findByText('direct (env)', { selector: 'span' })
    expect(chip).toBeTruthy()
    // A state is a term, not a sentence. Every shipped locale renders this longer
    // than the English, and a badge broken across two ragged lines beside the
    // single-line `shared` and `direct` pills reads as unfinished UI.
    expect(chip.className).toContain('whitespace-nowrap')
    expect(screen.queryByText('shared', { selector: 'span' })).toBeNull()
  })

  it('surfaces why it is unwrapped, next to the chip', async () => {
    // The chip provokes "why is this not shared"; one line answers it in place.
    // The knob name lives in the table legend rather than repeating in every
    // blocked row -- the reason line is rendered always-visible, so a paragraph
    // there multiplies row height by the number of blocked servers.
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
    const reason = await screen.findByText(/not stubbed, but your opt-in stays saved/i)
    // The row carries ONLY what the legend cannot. Copying the cause into it made
    // two catalogs that must agree about one state in 13 locales -- a drift surface
    // for the same defect this change removes -- so the cause is defined once,
    // below, and is asserted there rather than here.
    expect(reason.textContent).not.toMatch(/withhold/i)
    const legend = screen.getByText(/direct \(env\) — the server declares an environment key/i)
    expect(legend).toBeTruthy()
    // The whole point of this state: on this branch the rewriter passes the
    // original spec through and never builds a stub. Copy that says the stub IS in
    // the path reproduces the very defect this change removes -- the page
    // reporting work the broker did not do -- and would tell the operator they
    // kept server-authored UI when they did not.
    expect(legend.textContent).toMatch(/does not stub it at all/i)
    expect(legend.textContent).toMatch(/no server-authored UI/i)
    // No remedy is promised. `forward_declared_env` defaults ON, so for the common
    // cause -- credential and rotating-secret keys, which are never forwarded --
    // naming that switch sends the operator to flip something already enabled. One
    // bool on the wire cannot tell the two causes apart, so it advises neither.
    expect(legend.textContent).not.toMatch(/forward_declared_env/i)
    // Same false claim, one card up on the same screen: the header count is
    // `len(stub_servers)` -- the operator's opt-in, not stubs the broker built --
    // so calling it "stubbed" contradicts the row that just said no stub exists.
    // Making the row honest is what made the two visibly disagree.
    expect(screen.queryByText(/of \d+ stubbed/i)).toBeNull()
    expect(screen.getByText(/opted in to stubbing/i)).toBeTruthy()
    // The row must not argue with the toggle one column over. STUB is ON here while
    // the sentence says no stub exists, and the rewriter takes the allowlist as a
    // read-only frozenset -- so the opt-in survives and will take effect once the
    // env obstacle clears. Without saying so, the obvious repair is to switch the
    // toggle off, which throws a saved intent away for nothing.
    expect(reason.textContent).toMatch(/opt-in stays saved/i)
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
    const chip = await screen.findByText('direct (env)', { selector: 'span' })
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
    const chip = await screen.findByText('shared', { selector: 'span' })
    expect(chip.className).toContain('--accent')
    expect(screen.queryByText(/not stubbed, but your opt-in stays saved/i)).toBeNull()
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
    const chip = await screen.findByText('direct (env)', { selector: 'span' })
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
    expect(await screen.findByText('direct', { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText('direct (env)', { selector: 'span' })).toBeNull()
    expect(screen.queryByText(/not stubbed, but your opt-in stays saved/i)).toBeNull()
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
    expect(await screen.findByText('direct (env)', { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText('stub', { selector: 'span' })).toBeNull()
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
    expect(await screen.findByText('shared', { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText('direct (env)', { selector: 'span' })).toBeNull()
    expect(screen.queryByText(/not stubbed, but your opt-in stays saved/i)).toBeNull()
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
    expect(await screen.findByText('shared', { selector: 'span' })).toBeTruthy()
    expect(screen.queryByText('direct (env)', { selector: 'span' })).toBeNull()
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
    expect(await screen.findByText('direct', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText('no stdio', { selector: 'span' })).toBeTruthy()
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
    const panel = await screen.findByRole('tabpanel')
    expect(panel.className).toContain('space-y-4')
  })
})
