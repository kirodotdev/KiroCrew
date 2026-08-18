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
import { render, screen, cleanup, waitFor } from '@testing-library/react'
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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
      ok: true,
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
    tab.click()
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
    expect(screen.getByText(/reads a credential whose value differs per session/i)).toBeTruthy()
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
    ;(await screen.findByRole('tab', { name: /sharing assessment/i })).click()

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
    ;(await screen.findByRole('tab', { name: /sharing assessment/i })).click()

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
