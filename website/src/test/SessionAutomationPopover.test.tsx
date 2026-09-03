import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SessionAutomationPopover from '../components/SessionAutomationPopover'
import {
  normalizeAutomationRecord,
  type AutomationRecord,
  type LegacyGoalLoop,
  type StructuredMonitor,
} from '../monitoring/automation'
import { api } from '../api/client'
import { structuredMonitorLoop } from './monitorFixtures'

const framerMocks = vi.hoisted(() => ({ reducedMotion: false }))

vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return { ...actual, useReducedMotion: () => framerMocks.reducedMotion }
})

vi.mock('../api/client', () => ({
  api: {
    monitorCreate: vi.fn(),
    monitorUpdate: vi.fn(),
    monitorStop: vi.fn(),
    monitorRestart: vi.fn(),
  },
}))

const activeMonitor: StructuredMonitor = {
  kind: 'structured_monitor', id: 'monitor-1', slotKey: 'chat-1', active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', cadenceSecs: 300,
  nextProbeAt: 1_800_000_300, wakeInstructions: 'Address actionable review feedback.',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'pending', reasonCode: 'checks_pending', observedAt: 1_800_000_000, decision: 'no_change' },
  usage: { probes: 5, wakes: 2, agentTurns: 2, inputTokens: 1200, outputTokens: 300, providerErrors: 1, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
}

const activeLegacyLoop: LegacyGoalLoop = {
  kind: 'legacy_goal_loop', id: 'legacy-1', slotKey: 'chat-1', message: 'Keep checking.',
  idleSecs: 300, maxCycles: 24, cycleCount: 2, active: true, lastFireAt: 0,
  nextDueAt: 1_900_000_000, maxRuntimeSecs: 14_400, stoppedReason: '',
}

function renderPopover(
  automation: AutomationRecord | null,
  onChange = vi.fn(),
  creationReady = true,
  sessionMode = '',
) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const props = (next: AutomationRecord | null, slotKey = 'chat-1') => (
    <QueryClientProvider client={client}>
      <SessionAutomationPopover
        slotKey={slotKey}
        automation={next}
        open
        onOpenChange={() => {}}
        onChange={onChange}
        creationReady={creationReady}
        sessionMode={sessionMode}
      />
    </QueryClientProvider>
  )
  const view = render(props(automation))
  return {
    client,
    onChange,
    ...view,
    rerenderAutomation: (next: AutomationRecord | null, slotKey?: string) => (
      view.rerender(props(next, slotKey))
    ),
  }
}

describe('SessionAutomationPopover', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    framerMocks.reducedMotion = false
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('creates a bounded review monitor with the documented defaults', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    const { client } = renderPopover(null)
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalledWith({
      slot_key: 'chat-1',
      kind: 'github_pull_request',
      objective: 'review_ready',
      target: 'https://github.com/kirodotdev/KiroCrew/pull/42',
      cadence_secs: 300,
      max_runtime_secs: 14_400,
      max_agent_turns: 8,
      max_tokens: 250_000,
      max_provider_errors: 3,
      wake_instructions: '',
    }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-automation', 'chat-1'] })
  })

  it('cannot create or enter legacy mode until the slot snapshot is authoritative', () => {
    renderPopover(null, vi.fn(), false)

    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Use legacy goal loop (costly)' }))
      .toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it.each(['crew', 'member'])('explains and disables creation in %s mode', sessionMode => {
    renderPopover(null, vi.fn(), true, sessionMode)

    expect(screen.getByText(
      "Monitors aren't available in crew or member sessions because those sessions route work through their crew.",
    )).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Use legacy goal loop (costly)' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it.each(['crew', 'member'])(
    'keeps legacy Stop available but disables legacy writes in %s mode',
    sessionMode => {
      const fetchMock = vi.fn()
      vi.stubGlobal('fetch', fetchMock)
      renderPopover(activeLegacyLoop, vi.fn(), true, sessionMode)

      expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Stop loop' })).toBeEnabled()
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(fetchMock).not.toHaveBeenCalled()
    },
  )

  it('keeps Stop reachable for a live monitor with an unrecognized wire value', () => {
    const invalid = normalizeAutomationRecord(structuredMonitorLoop({
      last_decision: 'future_decision',
    }))

    renderPopover(invalid)

    expect(screen.getByRole('button', { name: 'Stop monitor' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Restart monitor' })).toBeNull()
  })

  it('preserves the legacy loop deadline through the compatibility bridge', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_899_999_880_000)

    renderPopover(activeLegacyLoop)

    expect(screen.queryByText('Next cycle not yet scheduled')).toBeNull()
    expect(screen.getByText(/Next cycle in/)).toBeInTheDocument()
  })

  it('surfaces a failed cold snapshot while leaving the server-guarded legacy fallback enabled', () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <SessionAutomationPopover
          slotKey="chat-1"
          automation={null}
          open
          onOpenChange={() => {}}
          onChange={() => {}}
          creationReady={false}
          snapshotFailed
        />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load this session's monitor state. Retry loading before starting a monitor.")
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Use legacy goal loop (costly)' })).toBeEnabled()
  })

  it('announces a rejected request without losing the unsaved monitor draft', async () => {
    vi.mocked(api.monitorCreate).mockRejectedValueOnce(new Error('offline'))
    const { onChange } = renderPopover(null)
    const target = screen.getByRole('textbox', { name: 'Pull request URL' })
    fireEvent.change(target, { target: { value: 'https://github.com/acme/widgets/pull/42' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The monitor request failed. Try again.')
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    expect(target).toHaveValue('https://github.com/acme/widgets/pull/42')
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeEnabled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('retries a failed snapshot without permitting creation before the read succeeds', async () => {
    let resolveRead!: (value: null) => void
    const pendingRead = new Promise<null>(resolve => { resolveRead = resolve })
    const readSnapshot = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockReturnValueOnce(pendingRead)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    function SnapshotEditor() {
      const snapshot = useQuery({ queryKey: ['session-automation', 'chat-1'], queryFn: readSnapshot })
      return <SessionAutomationPopover
        slotKey="chat-1" automation={null} open onOpenChange={() => {}} onChange={() => {}}
        creationReady={snapshot.isSuccess && !snapshot.isFetching} snapshotFailed={snapshot.isError}
      />
    }
    const view = render(<QueryClientProvider client={client}><SnapshotEditor /></QueryClientProvider>)

    const retry = await screen.findByRole('button', { name: 'Retry loading' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/acme/widgets/pull/42' },
    })
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    fireEvent.click(retry)
    await waitFor(() => expect(readSnapshot).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    expect(api.monitorCreate).not.toHaveBeenCalled()
    await act(async () => { resolveRead(null); await pendingRead })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start monitor' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Retry loading' })).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Pull request URL' })).toHaveValue('https://github.com/acme/widgets/pull/42')
    view.unmount()
    client.clear()
  })

  it.each(['', '0', '-1', '1.5', 'NaN'])('rejects %j as an unbounded cadence', async value => {
    renderPopover(null)
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }), {
      target: { value },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('Enter a whole number from 15 to 86,400.')).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it.each([
    ['Probe cadence in seconds', '86401', 'Enter a whole number from 15 to 86,400.'],
    ['Maximum runtime in seconds', '604801', 'Enter a whole number from 1 to 604,800.'],
    ['Maximum agent turns', '9', 'Enter a whole number from 1 to 8.'],
    ['Maximum tokens', '1000001', 'Enter a whole number from 1 to 1,000,000.'],
    ['Maximum provider errors', '21', 'Enter a whole number from 1 to 20.'],
  ])('shows an inline backend-bound error for %s', async (name, value, message) => {
    renderPopover(null)
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name }), { target: { value } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it('exposes exact input bounds and rejects oversized wake instructions inline', async () => {
    renderPopover(null)

    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }))
      .toHaveAttribute('min', '15')
    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }))
      .toHaveAttribute('max', '86400')
    expect(screen.getByRole('spinbutton', { name: 'Maximum agent turns' }))
      .toHaveAttribute('max', '8')
    const wake = screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' })
    expect(wake).toHaveAttribute('maxlength', '1000')

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(wake, { target: { value: 'x'.repeat(1001) } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('Enter no more than 1,000 characters.')).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it('shows monitor evidence and requires confirmation before stopping', async () => {
    ;(api.monitorStop as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(activeMonitor)

    expect(screen.getByText('Probes: 5')).toBeInTheDocument()
    expect(screen.getByText('Wakes: 2')).toBeInTheDocument()
    expect(screen.getByText('Tokens: 1,500')).toBeInTheDocument()
    expect(screen.getByText('250,000')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Stop monitor' }))
    expect(api.monitorStop).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop' }))
    await waitFor(() => expect(api.monitorStop).toHaveBeenCalledWith('monitor-1'))
  })

  it('stacks monitor evidence on the narrowest viewport', () => {
    renderPopover(activeMonitor)

    expect(screen.getByText('Objective').closest('dl'))
      .toHaveClass('grid-cols-1', 'min-[390px]:grid-cols-2')
  })

  it('does not overwrite a newer websocket state with a mutation response', async () => {
    let resolveUpdate!: (value: { ok: true, monitor: Record<string, unknown> }) => void
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(resolve => {
      resolveUpdate = resolve
    }))
    const { onChange, rerenderAutomation } = renderPopover(activeMonitor)

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' }),
      { target: { value: 'Address the latest review.' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalled())

    rerenderAutomation({
      ...activeMonitor,
      active: false,
      actionable: false,
      terminal: { outcome: 'success', reason: 'review_ready', stoppedAt: 1_800_000_400 },
    })
    await act(async () => {
      resolveUpdate({ ok: true, monitor: structuredMonitorLoop() })
      await Promise.resolve()
    })

    expect(onChange).not.toHaveBeenCalled()
  })

  it('invalidates the originating slot when selection changes during a save', async () => {
    let resolveUpdate!: (value: { ok: true, monitor: Record<string, unknown> }) => void
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(resolve => {
      resolveUpdate = resolve
    }))
    const { client, rerenderAutomation } = renderPopover(activeMonitor)
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' }),
      { target: { value: 'Address the latest review.' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalled())

    rerenderAutomation({ ...activeMonitor, id: 'monitor-2', slotKey: 'chat-2' }, 'chat-2')
    await act(async () => {
      resolveUpdate({ ok: true, monitor: structuredMonitorLoop() })
      await Promise.resolve()
    })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-automation', 'chat-1'] })
  })

  it('disables draft fields while a save is pending', async () => {
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    renderPopover(activeMonitor)
    const instructions = screen.getByRole('textbox', {
      name: 'Instructions for the agent when it wakes',
    })

    fireEvent.change(instructions, { target: { value: 'Address the latest review.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalled())

    expect(instructions).toBeDisabled()
  })

  it('does not overwrite a newer structured monitor with a delayed legacy response', async () => {
    let resolveFetch!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => {
      resolveFetch = resolve
    })))
    const { onChange, rerenderAutomation } = renderPopover(activeLegacyLoop)

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())

    rerenderAutomation(activeMonitor)
    await act(async () => {
      resolveFetch(new Response(JSON.stringify({
        loop: {
          id: 'legacy-1', slot_key: 'chat-1', message: 'Keep checking.',
          idle_secs: 300, max_cycles: 24, cycle_count: 2, active: true,
          last_fire_ts: 0,
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      await Promise.resolve()
    })

    expect(onChange).not.toHaveBeenCalled()
  })

  it('applies a mutation response when the captured automation is still current', async () => {
    const response = structuredMonitorLoop({
      wake_instructions: 'Address the latest review.',
    })
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      monitor: response,
    })
    const { onChange } = renderPopover(activeMonitor)

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' }),
      { target: { value: 'Address the latest review.' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(normalizeAutomationRecord(response))
    })
  })

  it('does not resurrect a cleared monitor from a delayed create response', async () => {
    let resolveCreate!: (value: unknown) => void
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockImplementation(() => (
      new Promise(resolve => { resolveCreate = resolve })
    ))
    const { client, onChange, rerenderAutomation } = renderPopover(null)
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))
    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalled())

    rerenderAutomation(activeMonitor)
    rerenderAutomation(null)
    await act(async () => {
      resolveCreate({ ok: true, monitor: structuredMonitorLoop() })
      await Promise.resolve()
    })

    expect(onChange).not.toHaveBeenCalled()
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-automation', 'chat-1'] })
  })

  it('renders typed classification without decoding canonical provider facts', () => {
    const record = normalizeAutomationRecord(structuredMonitorLoop())
    expect(record?.kind).toBe('structured_monitor')

    renderPopover(record as StructuredMonitor)

    expect(screen.getByText('pending · checks_pending')).toBeInTheDocument()
  })

  it('uses the static Framer state under reduced motion and the shared Lucide seam', () => {
    framerMocks.reducedMotion = true
    const { container } = renderPopover({
      ...activeMonitor,
      action: { wakeInFlight: true, wakeDelivery: 'dispatched' },
    })

    expect(container.querySelector('[data-monitor-action-pulse="false"]')).toBeTruthy()
    for (const icon of container.querySelectorAll('.lucide-radar, .lucide-x, .lucide-square, .lucide-activity')) {
      expect(icon).toHaveClass('lucide-inline')
    }
    expect(container.querySelector('.animate-pulse')).toBeNull()
  })

  it('keeps dirty fields while reconciling untouched fields and sends a sparse update', async () => {
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, monitor: {},
    })
    const { rerenderAutomation } = renderPopover(activeMonitor)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/99' },
    })
    rerenderAutomation({
      ...activeMonitor,
      cadenceSecs: 600,
      wakeInstructions: 'Use the latest server instructions.',
    })

    expect(screen.getByRole('textbox', { name: 'Pull request URL' })).toHaveValue(
      'https://github.com/kirodotdev/KiroCrew/pull/99',
    )
    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' })).toHaveValue(600)
    expect(screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' }))
      .toHaveValue('Use the latest server instructions.')

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalledWith('monitor-1', {
      target: 'https://github.com/kirodotdev/KiroCrew/pull/99',
    }))
  })

  it('does not submit unchanged monitor values', () => {
    renderPopover(activeMonitor)

    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(api.monitorUpdate).not.toHaveBeenCalled()
  })

  it('keeps terminal monitors read-only and revives them only through Restart', async () => {
    ;(api.monitorRestart as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover({
      ...activeMonitor,
      active: false,
      terminal: { outcome: 'budget', reason: 'token_budget', stoppedAt: 1_800_000_100 },
    })

    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop monitor' })).not.toBeInTheDocument()
    expect(screen.getAllByText('budget stopped')).toHaveLength(2)
    expect(screen.getByText('token_budget')).toHaveClass('font-mono')
    expect(screen.getByText('Address actionable review feedback.')).toBeInTheDocument()
    expect(screen.getByText('250,000')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart monitor' }))
    await waitFor(() => expect(api.monitorRestart).toHaveBeenCalledWith('monitor-1'))
  })

  it('wraps unbroken terminal wake instructions on narrow layouts', () => {
    const instructions = 'a'.repeat(1000)
    renderPopover({
      ...activeMonitor,
      active: false,
      wakeInstructions: instructions,
      terminal: { outcome: 'budget', reason: 'token_budget', stoppedAt: 1_800_000_100 },
    })

    expect(screen.getByText(instructions)).toHaveClass('break-words')
  })

  it('offers the old costly loop explicitly without changing zero-unlimited semantics', () => {
    renderPopover(null)
    fireEvent.click(screen.getByRole('button', { name: 'Use legacy goal loop (costly)' }))

    const notice = screen.getByText(
      'This legacy loop invokes the agent every cycle and can run without a limit.',
    )
    const panel = notice.closest('[data-side]')
    const maxCycles = screen.getByRole('spinbutton', { name: 'Max cycles (0 = infinite)' })
    expect(notice).toBeInTheDocument()
    expect(panel).toHaveClass(
      'w-[min(calc(100vw-1rem),26.25rem)]',
      'max-h-[min(80vh,42rem)]',
      'overflow-y-auto',
    )
    expect(maxCycles).toHaveValue(0)
    expect(maxCycles.parentElement?.parentElement).toHaveClass('flex-col', 'sm:flex-row')

    fireEvent.click(screen.getByRole('button', { name: 'Back to bounded monitor' }))
    expect(screen.getByRole('textbox', { name: 'Pull request URL' })).toBeInTheDocument()
  })
})
