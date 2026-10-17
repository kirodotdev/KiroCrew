import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { flushSync } from 'react-dom'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SessionAutomationPopover from '../components/SessionAutomationPopover'
import {
  normalizeAutomationRecord,
  type AutomationRecord,
  type LegacyGoalLoop,
  type StructuredMonitor,
} from '../monitoring/automation'
import { api, ApiError } from '../api/client'
import { structuredMonitorLoop } from './monitorFixtures'

const framerMocks = vi.hoisted(() => ({ reducedMotion: false }))

vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return { ...actual, useReducedMotion: () => framerMocks.reducedMotion }
})

vi.mock('../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../api/client')>(),
  api: {
    monitorForSlot: vi.fn(),
    monitorCreate: vi.fn(),
    monitorUpdate: vi.fn(),
    monitorStop: vi.fn(),
    monitorClear: vi.fn(),
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

/* The popover opens on the goal loop, so a test about the BOUNDED form has to
   walk to it exactly as a reader does. Pressed only when the offer is on
   screen: a slot that already holds a monitor opens on the bounded view, and a
   slot running a legacy loop renders no offer at all, so both cases must reach
   their view without a click rather than fail looking for one. */
function enterBoundedView() {
  const offer = screen.queryByRole('button', { name: 'Watch a pull request instead' })
  if (offer) fireEvent.click(offer)
}

function renderPopover(
  automation: AutomationRecord | null,
  onChange = vi.fn(),
  creationReady = true,
  onOpenChange = vi.fn(),
  sessionMode = '',
  { enterBounded = true }: { enterBounded?: boolean } = {},
) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const props = (next: AutomationRecord | null, slotKey = 'chat-1', open = true) => (
    <QueryClientProvider client={client}>
      <SessionAutomationPopover
        slotKey={slotKey}
        automation={next}
        open={open}
        onOpenChange={onOpenChange}
        onChange={onChange}
        creationReady={creationReady}
        sessionMode={sessionMode}
      />
    </QueryClientProvider>
  )
  const view = render(props(automation))
  if (enterBounded) enterBoundedView()
  return {
    client,
    onChange,
    onOpenChange,
    ...view,
    rerenderAutomation: (next: AutomationRecord | null, slotKey?: string, open?: boolean) => (
      view.rerender(props(next, slotKey, open))
    ),
  }
}

describe('SessionAutomationPopover', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    framerMocks.reducedMotion = false
    /* The bounded view reads the live runtime ceiling off the per-slot monitor
       read. Default it to the contract's absolute maximum so the existing
       bound assertions keep describing the contract; the ceiling tests below
       override it per case. */
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: true, monitor: null, max_runtime_ceiling_secs: 2_592_000,
    })
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
      max_agent_turns: 0,
      max_tokens: 250_000,
      max_provider_errors: 3,
      wake_instructions: '',
    }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-automation', 'chat-1'] })
  })

  it('cannot create or enter legacy mode until the slot snapshot is authoritative', () => {
    renderPopover(null, vi.fn(), false)

    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Back to goal loop' }))
      .toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it.each(['crew', 'member'])('explains and disables creation in %s mode', sessionMode => {
    renderPopover(null, vi.fn(), true, vi.fn(), sessionMode)

    expect(screen.getByText(
      "Automations aren't available in crew or member sessions because those sessions route work through their crew.",
    )).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    /* The way BACK is never gated: it writes nothing, so an unsupported mode
       has nothing to refuse. Exercised as a round trip in its own case below. */
    expect(screen.getByRole('button', { name: 'Back to goal loop' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it.each(['crew', 'member'])('leaves a %s session a way back off the bounded form', sessionMode => {
    renderPopover(null, vi.fn(), true, vi.fn(), sessionMode)

    /* The offer that brings a reader here carries no mode gate, so gating the
       exit stranded them on this form with Close as the only move. */
    fireEvent.click(screen.getByRole('button', { name: 'Back to goal loop' }))

    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Pull request URL' })).not.toBeInTheDocument()
  })

  it.each(['crew', 'member'])(
    'disables the goal fields, Pause and Play in %s mode and writes nothing',
    sessionMode => {
      const fetchMock = vi.fn()
      vi.stubGlobal('fetch', fetchMock)
      renderPopover(activeLegacyLoop, vi.fn(), true, vi.fn(), sessionMode)

      expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Pause loop' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Nudge now' })).toBeDisabled()
      fireEvent.click(screen.getByRole('button', { name: 'Nudge now' }))
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

  it.each([
    ['manual', 'Paused · you paused it'],
    ['cycle_cap', /^Paused · cycle limit reached \(2 of 24\)\./],
  ])('carries stopped_reason %s through the compatibility bridge, so the paused line names it and Play stays live', (reason, line) => {
    // A bridge that drops the field renders every inactive loop, a fresh pause
    // included, as a bare "Paused".
    renderPopover({ ...activeLegacyLoop, active: false, nextDueAt: 0, stoppedReason: reason })
    expect(screen.getByTestId('auto-nudge-status')).toHaveTextContent(line)
    expect(screen.getByRole('button', { name: 'Resume loop and nudge now' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Clear stopped goal' })).toBeInTheDocument()
  })

  it('carries a held loop through the compatibility bridge: it reads paused and Play resumes it', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_899_999_880_000)
    // A bridge that drops approval_stalled shows "next cycle due" and disables
    // Play, which leaves the popover no way to end the hold.
    renderPopover({ ...activeLegacyLoop, nextDueAt: 1_899_999_000, approvalStalled: true })
    expect(screen.getByTestId('auto-nudge-status')).toHaveTextContent(/^Paused · an approval in this chat timed out\./)
    expect(screen.getByRole('button', { name: 'Resume loop and nudge now' })).toBeEnabled()
  })

  it('carries a person-wait hold through the compatibility bridge', () => {
    renderPopover({ ...activeLegacyLoop, waitingOnPerson: true })
    expect(screen.getByTestId('auto-nudge-status')).toHaveTextContent(/^Waiting on you · /)
  })

  it('clears a paused legacy loop through the bridge: the record is handed up as null and the popover closes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    const { onChange, onOpenChange } = renderPopover({ ...activeLegacyLoop, active: false, nextDueAt: 0, stoppedReason: 'manual' })
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear' })) })
    expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1?intent=clear', { method: 'DELETE' })
    expect(onChange).toHaveBeenCalledWith(null)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('centres the radar glyph and its count in the composer trigger', () => {
    // IconButton is a plain block button: without a flex row the inline glyph
    // sits on the text baseline of the 32px box instead of at its centre.
    renderPopover(activeMonitor, vi.fn(), true, '', vi.fn())

    const trigger = screen.getByRole('button', { name: 'Monitor status: active' })
    expect(trigger.className.split(/\s+/)).toEqual(
      expect.arrayContaining(['h-8', 'flex', 'items-center', 'gap-1']),
    )
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
    enterBoundedView()

    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load this session's monitor state. Retry loading before starting a monitor.")
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Back to goal loop' })).toBeEnabled()
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
    enterBoundedView()

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

  it.each([
    ['https://gitlab.com/acme/widgets/-/merge_requests/2', 'gitlab_merge_request'],
    ['https://dev.azure.com/acme/project/_git/widgets/pullrequest/3', 'azure_devops_pull_request'],
    ['https://bitbucket.org/acme/widgets/pull-requests/4', 'bitbucket_pull_request'],
  ])('creates a bounded %s monitor', async (target, kind) => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: target },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalledWith(
      expect.objectContaining({ kind, target }),
    ))
  })

  it('canonicalizes a provider subtab before creating the monitor', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://gitlab.com/acme/widgets/-/merge_requests/2/diffs' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'gitlab_merge_request',
      target: 'https://gitlab.com/acme/widgets/-/merge_requests/2',
    })))
  })

  it('canonicalizes copied pull request links with query strings and fragments', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: {
        value: 'https://github.com/kirodotdev/KiroCrew/pull/42?notification_referrer_id=1#discussion_r2',
      },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalledWith(expect.objectContaining({
      target: 'https://github.com/kirodotdev/KiroCrew/pull/42',
    })))
  })

  it('names all supported source providers at the target field', () => {
    renderPopover(null)

    expect(
      screen.getByText(
        'GitHub.com, GitLab, Azure DevOps Services, and Bitbucket Cloud are supported.',
      ),
    )
      .toBeInTheDocument()
  })

  it('distinguishes an invalid target from a provider-changing edit', async () => {
    const { rerenderAutomation } = renderPopover(null)
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://example.com/not-a-pull-request' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))
    expect(await screen.findByText('Enter a supported pull request URL.')).toBeInTheDocument()

    rerenderAutomation(activeMonitor)
    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://gitlab.com/acme/widgets/-/merge_requests/2' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText(
      'This monitor is tied to its current code host. Watch the new link from a new session.',
    ))
      .toBeInTheDocument()
  })

  it('keeps the required URL error when an existing target is cleared', async () => {
    renderPopover(activeMonitor)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: '' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('Enter a pull request URL.')).toBeInTheDocument()
    expect(screen.queryByText(
      'This monitor is tied to its current code host. Watch the new link from a new session.',
    ))
      .not.toBeInTheDocument()
  })

  it('explains the GitLab allowlist when the backend rejects a valid custom host', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(
      400,
      'Bad Request',
      JSON.stringify({ code: 'gitlab_host_not_allowed', error: 'target is not allowed' }),
    ))
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://git.example:8443/acme/widgets/-/merge_requests/2' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText(
      "This GitLab host isn't allowed yet. Add it to dashboard.gitlab_hosts in ~/.kiro/crew/config.json.",
    )).toBeInTheDocument()
  })

  it('does not guess an allowlist error from a generic backend rejection', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(
      400,
      'Bad Request',
      JSON.stringify({ code: 'invalid_monitor', error: 'wake instructions are invalid' }),
    ))
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://git.example/acme/widgets/-/merge_requests/2' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('The monitor request failed. Try again.')).toBeInTheDocument()
    expect(screen.queryByText(
      "This GitLab host isn't allowed yet. Add it to dashboard.gitlab_hosts in ~/.kiro/crew/config.json.",
    )).not.toBeInTheDocument()
  })

  it('shows the URL error when the backend rejects a provider-specific target', async () => {
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(
      400,
      'Bad Request',
      JSON.stringify({ code: 'invalid_pull_request_url', error: 'target is not allowed' }),
    ))
    renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: {
        value: 'https://dev.azure.com/acme/Bad~Project/_git/widgets/pullrequest/9',
      },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('Enter a supported pull request URL.')).toBeInTheDocument()
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
    ['Maximum runtime in seconds', '604801', 'Enter a whole number from 1 to 604,800 (7 days).'],
    ['Maximum agent turns', '1001', 'Enter a whole number from 0 to 1,000.'],
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

  it('bounds the runtime input by the live operator ceiling, not the contract maximum', async () => {
    /* A default install's server enforces `monitoring.max_runtime_secs`
       (seven days) while contract.json advertises the 30-day absolute maximum.
       Validating against the contract alone lets a value through that can only
       fail after submit as an HTTP 400; the popover must refuse it inline. */
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: true, monitor: null, max_runtime_ceiling_secs: 604_800,
    })
    renderPopover(null)
    const runtime = screen.getByRole('spinbutton', { name: 'Maximum runtime in seconds' })
    await waitFor(() => expect(runtime).toHaveAttribute('max', '604800'))
    expect(api.monitorForSlot).toHaveBeenCalledWith('chat-1')

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(runtime, { target: { value: '604801' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText('Enter a whole number from 1 to 604,800 (7 days).')).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it('accepts a runtime the raised operator ceiling permits', async () => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: true, monitor: null, max_runtime_ceiling_secs: 2_592_000,
    })
    ;(api.monitorCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: {} })
    renderPopover(null)
    const runtime = screen.getByRole('spinbutton', { name: 'Maximum runtime in seconds' })
    await waitFor(() => expect(runtime).toHaveAttribute('max', '2592000'))

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(runtime, { target: { value: '2592000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => expect(api.monitorCreate).toHaveBeenCalledWith(
      expect.objectContaining({ max_runtime_secs: 2_592_000 }),
    ))
  })

  it.each([
    [2_592_000, '2592001', 'Enter a whole number from 1 to 2,592,000 (30 days).'],
    [5_400, '5401', 'Enter a whole number from 1 to 5,400 (90 minutes).'],
    [90, '91', 'Enter a whole number from 1 to 90 (90 seconds).'],
  ])('glosses the runtime ceiling %s with the largest unit that divides it exactly', async (ceiling, value, message) => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: true, monitor: null, max_runtime_ceiling_secs: ceiling,
    })
    renderPopover(null)
    const runtime = screen.getByRole('spinbutton', { name: 'Maximum runtime in seconds' })
    await waitFor(() => expect(runtime).toHaveAttribute('max', String(ceiling)))

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/kirodotdev/KiroCrew/pull/42' },
    })
    fireEvent.change(runtime, { target: { value } })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(api.monitorCreate).not.toHaveBeenCalled()
  })

  it('uses the shipped runtime ceiling while the live ceiling read is pending', () => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    renderPopover(null)

    expect(screen.getByRole('spinbutton', { name: 'Maximum runtime in seconds' }))
      .toHaveAttribute('max', '604800')
  })

  it('keeps the shipped runtime ceiling and renders the read failure', async () => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'))
    renderPopover(null)

    const notice = await screen.findByTestId('monitor-read-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(
      "Couldn't load this session's monitor state. Retry loading before starting a monitor.",
    )
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry loading' })).toBeEnabled()
    expect(screen.getByRole('spinbutton', { name: 'Maximum runtime in seconds' }))
      .toHaveAttribute('max', '604800')
  })

  it('does not stack the ceiling read failure under a rejected request', async () => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'))
    vi.mocked(api.monitorCreate).mockRejectedValueOnce(new Error('offline'))
    renderPopover(null)
    expect(await screen.findByTestId('monitor-read-error')).toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/acme/widgets/pull/42' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start monitor' }))

    await waitFor(() => {
      const alerts = screen.getAllByRole('alert')
      expect(alerts).toHaveLength(1)
      expect(alerts[0]).toHaveTextContent('The monitor request failed. Try again.')
    })
    expect(screen.queryByTestId('monitor-read-error')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry loading' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Start monitor' })).toBeEnabled()
  })

  it('renders one notice when the snapshot and the ceiling read fail together and retries both', async () => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue({ enabled: true, monitor: null, max_runtime_ceiling_secs: 604_800 })
    const readSnapshot = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(null)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    function SnapshotEditor() {
      const snapshot = useQuery({ queryKey: ['session-automation', 'chat-1'], queryFn: readSnapshot })
      return <SessionAutomationPopover
        slotKey="chat-1" automation={null} open onOpenChange={() => {}} onChange={() => {}}
        creationReady={snapshot.isSuccess && !snapshot.isFetching} snapshotFailed={snapshot.isError}
      />
    }
    const view = render(<QueryClientProvider client={client}><SnapshotEditor /></QueryClientProvider>)
    enterBoundedView()

    await waitFor(() => expect(readSnapshot).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(api.monitorForSlot).toHaveBeenCalledTimes(1))
    const retry = await screen.findByRole('button', { name: 'Retry loading' })
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Retry loading' })).toHaveLength(1)

    fireEvent.click(retry)
    await waitFor(() => expect(readSnapshot).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(api.monitorForSlot).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.queryByRole('button', { name: 'Retry loading' })).toBeNull()
    view.unmount()
    client.clear()
  })

  it('does not read the runtime ceiling for the goal-loop view', () => {
    renderPopover(activeLegacyLoop)
    expect(api.monitorForSlot).not.toHaveBeenCalled()
  })

  it('exposes exact input bounds and rejects oversized wake instructions inline', async () => {
    renderPopover(null)

    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }))
      .toHaveAttribute('min', '15')
    expect(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }))
      .toHaveAttribute('max', '86400')
    expect(screen.getByRole('spinbutton', { name: 'Maximum agent turns' }))
      .toHaveAttribute('max', '1000')
    // Floor 0: this budget's unlimited sentinel.
    expect(screen.getByRole('spinbutton', { name: 'Maximum agent turns' }))
      .toHaveAttribute('min', '0')
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

  it('renders an unlimited wake budget as a word, not as 0', () => {
    // 0 is the sentinel, so the digit says the opposite of the meaning: read under
    // a "Maximum agent turns" label it claims no wake is allowed.
    renderPopover({
      ...activeMonitor,
      budgets: { ...activeMonitor.budgets, maxAgentTurns: 0 },
    })

    expect(screen.getByText('Unlimited')).toBeInTheDocument()
  })

  it('keeps rendering a finite wake budget as its number', () => {
    renderPopover({
      ...activeMonitor,
      budgets: { ...activeMonitor.budgets, maxAgentTurns: 6 },
    })

    expect(screen.getByText('6')).toBeInTheDocument()
    expect(screen.queryByText('Unlimited')).not.toBeInTheDocument()
  })

  it('tells the create form what a wake budget of 0 means', () => {
    // Entering 0 on a "maximum" reads as "no turns allowed" without this hint,
    // so the sentinel's meaning is spelled out beside the field.
    renderPopover(null)

    expect(screen.getByText('0 = no wake ceiling')).toBeInTheDocument()
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

  it('closes after stopping a provider-changing monitor without replacing it', async () => {
    const stopped = {
      ...structuredMonitorLoop({
        active: false,
        outcome: 'user_stop',
        stopped_reason: 'user_stop',
        stopped_at: 1_800_000_400,
      }),
      id: 'monitor-1',
    }
    let resolveStop: ((value: { ok: boolean; monitor: typeof stopped }) => void) | undefined
    ;(api.monitorStop as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise(resolve => { resolveStop = resolve }),
    )
    const onOpenChange = vi.fn()
    const onChange = vi.fn()
    const view = renderPopover(activeMonitor, onChange, true, onOpenChange)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://gitlab.com/acme/widgets/-/merge_requests/8' },
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }), {
      target: { value: '600' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText(
      'This monitor is tied to its current code host. Watch the new link from a new session.',
    )).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Stop monitor' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop' }))

    await waitFor(() => expect(api.monitorStop).toHaveBeenCalledWith('monitor-1'))
    const terminalAutomation = normalizeAutomationRecord(stopped)
    expect(terminalAutomation).not.toBeNull()
    view.rerenderAutomation(terminalAutomation)
    await act(async () => resolveStop?.({ ok: true, monitor: stopped }))

    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(api.monitorCreate).not.toHaveBeenCalled()
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

  it('keeps the popover open while a save is pending', async () => {
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    const onOpenChange = vi.fn()
    renderPopover(activeMonitor, vi.fn(), true, onOpenChange)

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' }),
      { target: { value: 'Address the latest review.' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalled())

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('retains a pending draft and its late error for the originating slot', async () => {
    let rejectUpdate!: (error: Error) => void
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockReturnValue(new Promise((_, reject) => {
      rejectUpdate = reject
    }))
    const { rerenderAutomation } = renderPopover(activeMonitor)
    const submitted = 'Address the latest review before reporting.'

    fireEvent.change(
      screen.getByRole('textbox', { name: 'Instructions for the agent when it wakes' }),
      { target: { value: submitted } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalled())

    rerenderAutomation({ ...activeMonitor, id: 'monitor-2', slotKey: 'chat-2' }, 'chat-2', false)
    await act(async () => {
      rejectUpdate(new Error('offline'))
      await Promise.resolve()
    })
    rerenderAutomation(activeMonitor, 'chat-1', true)

    expect(screen.getByRole('textbox', {
      name: 'Instructions for the agent when it wakes',
    })).toHaveValue(submitted)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The monitor request failed. Try again.',
    )
  })

  it('does not overwrite a newer structured monitor with a delayed legacy response', async () => {
    let resolveFetch!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => {
      resolveFetch = resolve
    })))
    const { onChange, rerenderAutomation } = renderPopover(activeLegacyLoop)

    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' }))
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

  it('replaces a bounded draft with a legacy loop that arrives while open', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      loop: {
        id: 'legacy-1', slot_key: 'chat-1', message: 'Keep checking.',
        idle_secs: 300, max_cycles: 24, cycle_count: 2, active: true,
        last_fire_ts: 0,
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { rerenderAutomation } = renderPopover(null)

    fireEvent.change(screen.getByRole('textbox', { name: 'Pull request URL' }), {
      target: { value: 'https://github.com/acme/widgets/pull/42' },
    })
    rerenderAutomation(activeLegacyLoop)

    await waitFor(() => {
      expect(screen.getByRole('textbox', { name: 'Goal description' }))
        .toHaveValue('Keep checking.')
    })
    expect(screen.getByRole('spinbutton', { name: 'Seconds between nudges' })).toHaveValue(300)
    expect(screen.getByRole('spinbutton', { name: 'Max cycles (0 = infinite)' })).toHaveValue(24)

    // Edit one field, then Play: the write goes to the LEGACY loop's id and
    // carries the edited field only, never `active` on a running loop.
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), {
      target: { value: 'Keep checking, closely.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'Keep checking, closely.' }),
    }))
  })

  it('a two-leg press (write, then fire) reaches the parent twice, each leg with only what it owns: the written record at the write, then the armed deadline on the record the parent holds', async () => {
    // ChatPage re-identifies `automation` on every hand-off
    // (`dispatch(sseAutomation(next))`), so the record the parent holds moves
    // under a press that has two legs. The write leg hands its record up the
    // moment the write lands -- the same hand-off Save and Pause make -- and
    // the fire leg hands up NO record at all: it reports the fire (`onFired`)
    // and this bridge arms the deadline on whatever record the parent holds
    // NOW (`armedNow`). The bridge applies the write's record only while the
    // parent's record has not moved since the press (and re-reads the slot
    // otherwise), so nothing a press hands up can be older than the parent's
    // record, and the schedule reads due on the parent's own record.
    // `flushSync` stands in for the store notification, which re-renders the
    // parent before the next leg's response can arrive.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 0, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const answers = init?.method === 'PATCH' || /\/fire$/.test(String(url))
      return Promise.resolve(new Response(JSON.stringify(answers ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const received: (AutomationRecord | null)[] = []
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    function Parent() {
      const [automation, setAutomation] = useState<AutomationRecord | null>(activeLegacyLoop)
      return (
        <QueryClientProvider client={client}>
          <SessionAutomationPopover
            slotKey="chat-1"
            automation={automation}
            open={true}
            onOpenChange={() => {}}
            onChange={next => { received.push(next); flushSync(() => setAutomation(next)) }}
            creationReady={true}
            sessionMode=""
          />
        </QueryClientProvider>
      )
    }
    render(<Parent />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    await waitFor(() => expect(received).toHaveLength(2))
    // The write leg's own record, as the server returned it: the saved goal
    // on a fresh full countdown.
    const saved = received[0] as LegacyGoalLoop
    expect(saved.kind).toBe('legacy_goal_loop')
    expect(saved.message).toBe('edited')
    expect(saved.nextDueAt).toBe(1_900_000_300)
    // The fire leg: the record the parent holds (the saved one), with the
    // deadline armed to now -- not the fire response, which the server
    // returns unchanged.
    const armed = received[1] as LegacyGoalLoop
    expect(armed.kind).toBe('legacy_goal_loop')
    expect(armed.message).toBe('edited')
    expect(Math.abs((armed.nextDueAt ?? 0) - Date.now() / 1000)).toBeLessThan(5)
  })

  /** A parent that holds `automation` as state, with a `frame` hook the fetch
   *  mock can pull to re-identify the record mid-press -- the way the write
   *  leg's `autonudge_state` frame reaches the store (and this bridge's prop)
   *  before the fire leg's response does. */
  function interleavedParent(fetchMock: ReturnType<typeof vi.fn>) {
    const received: (AutomationRecord | null)[] = []
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const frame: { current: ((record: AutomationRecord) => void) | null } = { current: null }
    function Parent() {
      const [automation, setAutomation] = useState<AutomationRecord | null>(activeLegacyLoop)
      frame.current = record => flushSync(() => setAutomation(record))
      return (
        <QueryClientProvider client={client}>
          <SessionAutomationPopover
            slotKey="chat-1"
            automation={automation}
            open={true}
            onOpenChange={() => {}}
            onChange={next => { received.push(next); flushSync(() => setAutomation(next)) }}
            creationReady={true}
            sessionMode=""
          />
        </QueryClientProvider>
      )
    }
    vi.stubGlobal('fetch', fetchMock)
    render(<Parent />)
    return { received, frame }
  }

  it('a frame that re-identifies the SAME loop between the write leg and the fire leg is what the fire arms: the deadline lands on the frame\'s record', async () => {
    // The write leg's PATCH makes the service emit `updated`, which the gateway
    // broadcasts as an `autonudge_state` frame; the store dispatch hands this
    // bridge a NEW `automation` object for the same loop before the fire leg's
    // response arrives. A guard on object identity once dropped the press's
    // hand-off there, so the parent kept the frame's full countdown over a
    // cycle armed to run now, with Nudge now still enabled (GPT, head
    // 5b284e13c7). Now the fire arms the deadline on the record the parent
    // holds -- the frame's -- so the frame costs the press nothing and the
    // parent never receives a record older than the frame.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 1_899_999_700, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const fromFrame = { ...written, message: 'edited, as the frame carries it' }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (/\/fire$/.test(String(url))) frame.current?.(normalizeAutomationRecord({ ...fromFrame }) as AutomationRecord)
      const answers = init?.method === 'PATCH' || /\/fire$/.test(String(url))
      return Promise.resolve(new Response(JSON.stringify(answers ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    const { received, frame } = interleavedParent(fetchMock)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    await waitFor(() => expect(received).toHaveLength(2))
    // The write's own hand-off came first, before the frame.
    expect((received[0] as LegacyGoalLoop).message).toBe('edited')
    // The fire armed the FRAME's record, not the response's: its fields, the
    // armed deadline.
    const armed = received[1] as LegacyGoalLoop
    expect(armed.kind).toBe('legacy_goal_loop')
    expect(armed.id).toBe('legacy-1')
    expect(armed.message).toBe('edited, as the frame carries it')
    expect(Math.abs((armed.nextDueAt ?? 0) - Date.now() / 1000)).toBeLessThan(5)
  })

  it('a fire the frame already delivered has nothing left to arm: the parent keeps the delivered record', async () => {
    // The opposite interleaving: the service fired and its `fired` frame landed
    // (count up, deadline a full interval away) before the fire leg's response
    // was handled. Arming "now" on that record would roll the count's deadline
    // back and read "Next cycle due" for a cycle already delivered, until the
    // following frame a whole interval later. A loop that fired since the
    // press keeps the frame; only the write's own hand-off reaches the parent.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 1_899_999_700, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const nowTs = Math.floor(Date.now() / 1000)
    const delivered = { ...written, cycle_count: 3, last_fire_ts: nowTs, next_due_ts: nowTs + 300 }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (/\/fire$/.test(String(url))) frame.current?.(normalizeAutomationRecord({ ...delivered }) as AutomationRecord)
      const answers = init?.method === 'PATCH' || /\/fire$/.test(String(url))
      return Promise.resolve(new Response(JSON.stringify(answers ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    const { received, frame } = interleavedParent(fetchMock)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    // Let the fire leg settle: only the write's hand-off may have reached the parent.
    await act(async () => { await Promise.resolve() })
    expect(received).toHaveLength(1)
    expect((received[0] as LegacyGoalLoop).nextDueAt).toBe(1_900_000_300)
    // The delivered frame's reading stands: cycle 3 of 24 in the trigger's name.
    expect(screen.getByRole('button', { name: /Goal active \(cycle 3\/24\)/ })).toBeTruthy()
  })

  it('a fire whose answer is serialized AFTER the delivery -- its last_fire_ts equal to the fired frame\'s -- has nothing left to arm either: the record the press was made on is the baseline, not the answer', async () => {
    // GPT, head cd2c53e472 (:170): `fire_now` arms a zero-delay timer and
    // returns the LIVE loop object, and the route serializes it only after
    // its audit await -- so a delivery that completes inside that window makes
    // the answer post-delivery, its `last_fire_ts` EQUAL to the `fired`
    // frame's. A guard that compared the parent's record against the answer
    // read the two as the same fire, armed "now" on the delivered record, and
    // the schedule read due for a whole interval. The press reports the record
    // it was made on -- the written record here, pre-request by construction
    // -- and a parent record that has fired since it leaves nothing to arm.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 1_899_999_700, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const nowTs = Math.floor(Date.now() / 1000)
    const delivered = { ...written, cycle_count: 3, last_fire_ts: nowTs, next_due_ts: nowTs + 300 }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (/\/fire$/.test(String(url))) {
        frame.current?.(normalizeAutomationRecord({ ...delivered }) as AutomationRecord)
        // The answer, serialized after the delivery: the live loop as delivered.
        return Promise.resolve(new Response(JSON.stringify({ ok: true, loop: delivered }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }))
      }
      return Promise.resolve(new Response(JSON.stringify(init?.method === 'PATCH' ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    const { received, frame } = interleavedParent(fetchMock)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    await act(async () => { await Promise.resolve() })
    // Only the write's hand-off reached the parent; the delivered record was
    // not rolled back to due.
    expect(received).toHaveLength(1)
    expect((received[0] as LegacyGoalLoop).nextDueAt).toBe(1_900_000_300)
    expect(screen.getByRole('button', { name: /Goal active \(cycle 3\/24\)/ })).toBeTruthy()
  })

  it('a pause another writer landed between the write leg and the fire leg stands: the refused fire hands nothing up, the parent keeps the paused record', async () => {
    // GPT, head 02b38a4edc (:685): a second tab pauses the loop after this
    // tab's PATCH committed; the pause's inactive `autonudge_state` frame lands
    // in this tab's store, then `fire_now` answers 409 ("loop is not active").
    // The refusal path used to hand the WRITTEN record up -- active, read
    // moments before the pause -- and that stale snapshot overwrote the paused
    // frame, so the popover showed the paused loop as running until the next
    // frame or a reload. Now the write leg hands its record up when the write
    // lands, and a fire -- refused or not -- hands no record up at all, so the
    // pause is the last word.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 1_899_999_700, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const paused = { ...written, active: false, next_due_ts: 0, stopped_reason: 'manual' }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (/\/fire$/.test(String(url))) {
        frame.current?.(normalizeAutomationRecord({ ...paused }) as AutomationRecord)
        return Promise.resolve(new Response(JSON.stringify({ error: 'loop is not active' }), {
          status: 409, headers: { 'Content-Type': 'application/json' },
        }))
      }
      return Promise.resolve(new Response(JSON.stringify(init?.method === 'PATCH' ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    const { received, frame } = interleavedParent(fetchMock)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    await act(async () => { await Promise.resolve() })
    // The write's own hand-off, made before the pause landed, and nothing after it.
    expect(received).toHaveLength(1)
    expect((received[0] as LegacyGoalLoop).active).toBe(true)
    expect((received[0] as LegacyGoalLoop).message).toBe('edited')
    // The parent holds the pause: the trigger no longer reads active, the
    // popover shows the paused row, and the refusal landed in its notice.
    expect(screen.queryByRole('button', { name: /Goal active/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Set a goal' })).toBeTruthy()
    expect(screen.getByTestId('auto-nudge-status')).toHaveTextContent('Paused · you paused it')
    expect(screen.getByText('loop is not active')).toBeTruthy()
  })

  it('a pause that lands before an accepted fire\'s answer leaves nothing to arm: the parent keeps the paused record, no deadline is set on it', async () => {
    // Same interleaving, other side of the server's active check: the pause
    // lands after `fire_now` already answered 200, so the timer body skips the
    // cycle on its own re-check. The fire response is the record as it stood
    // BEFORE the pause (active, pre-delivery), and handing it up -- or arming a
    // deadline on a paused record -- would show a paused loop as running or
    // due. Nothing a press reports may outrank the parent's record.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 1_899_999_700, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const paused = { ...written, active: false, next_due_ts: 0, stopped_reason: 'manual' }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (/\/fire$/.test(String(url))) frame.current?.(normalizeAutomationRecord({ ...paused }) as AutomationRecord)
      const answers = init?.method === 'PATCH' || /\/fire$/.test(String(url))
      return Promise.resolve(new Response(JSON.stringify(answers ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    const { received, frame } = interleavedParent(fetchMock)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    await act(async () => { await Promise.resolve() })
    expect(received).toHaveLength(1)
    expect((received[0] as LegacyGoalLoop).active).toBe(true)
    expect(screen.queryByRole('button', { name: /Goal active/ })).toBeNull()
    expect(screen.getByTestId('auto-nudge-status')).toHaveTextContent('Paused · you paused it')
  })

  it('does not overwrite a newer frame of the SAME loop with a delayed write response: a pause another tab landed between this tab\'s save and its answer stands, and the slot is re-read', async () => {
    // GPT, head 9b921e2fa2 (:154): the record carries no revision, and the
    // parent writes whatever it is handed straight into the per-slot cache and
    // Redux. A guard that passed any response for the same loop id let a PATCH
    // answered BEFORE another tab's pause, but delivered after the pause's
    // `autonudge_state` frame, put the active record back over the paused one
    // -- and frames fire on change, so nothing corrected it until a reconnect.
    // The one thing this bridge can know is whether the parent's record moved
    // since the press was rendered: if it did, a frame landed, frames arrive in
    // the server's order and the write's own `updated` frame is among them or
    // follows, so the response is dropped and the slot's cold read is
    // invalidated instead -- the rule the bounded monitor's writes already
    // follow here.
    let resolveFetch!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => {
      resolveFetch = resolve
    })))
    const { client, onChange, rerenderAutomation } = renderPopover(activeLegacyLoop)
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())

    // The other tab's pause reaches the store first: same loop, inactive.
    rerenderAutomation({ ...activeLegacyLoop, active: false, nextDueAt: 0, stoppedReason: 'manual' })
    await act(async () => {
      resolveFetch(new Response(JSON.stringify({
        ok: true,
        loop: {
          id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
          cycle_count: 2, active: true, last_fire_ts: 0, next_due_ts: 1_900_000_300, stopped_reason: '',
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      await Promise.resolve()
    })

    // The stale active record never reaches the parent; the slot is re-read.
    // (Every write's success invalidates the shared loops query, so the first
    // wait is for the mutation to have settled at all; the assertions that
    // matter come after it.)
    await waitFor(() => expect(invalidate).toHaveBeenCalled())
    expect(onChange).not.toHaveBeenCalled()
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-automation', 'chat-1'] })
    // The pause is what the surface shows: the paused line, no active count.
    expect(screen.queryByRole('button', { name: /Goal active/ })).toBeNull()
    expect(screen.getByTestId('auto-nudge-status')).toHaveTextContent('Paused · you paused it')
  })

  it('the write leg\'s own frame landing before its response costs a two-leg press nothing: the response is dropped, and the fire still arms the frame\'s record', async () => {
    // The common ordering: the service broadcasts the write's `updated` frame
    // before the PATCH response is read, so the parent's record has moved --
    // to the written state -- by the time the write leg reports. The report is
    // dropped (the frame already carried it) and the fire leg arms the deadline
    // on the frame's record, so the parent receives exactly one record, never
    // one older than the frame, and the press still reads due.
    const written = {
      id: 'legacy-1', slot_key: 'chat-1', message: 'edited', idle_secs: 300, max_cycles: 24,
      cycle_count: 2, active: true, last_fire_ts: 1_899_999_700, next_due_ts: 1_900_000_300, stopped_reason: '',
    }
    const fromFrame = { ...written, message: 'edited, as the frame carries it' }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'PATCH') frame.current?.(normalizeAutomationRecord({ ...fromFrame }) as AutomationRecord)
      const answers = init?.method === 'PATCH' || /\/fire$/.test(String(url))
      return Promise.resolve(new Response(JSON.stringify(answers ? { ok: true, loop: written } : { loop: null }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    })
    const { received, frame } = interleavedParent(fetchMock)
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal description' }), { target: { value: 'edited' } })

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save edits and nudge now' })) })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1/fire', expect.objectContaining({ method: 'POST' })))
    await waitFor(() => expect(received).toHaveLength(1))
    await act(async () => { await Promise.resolve() })
    // One hand-off: the fire's, on the frame's record -- the written response
    // itself never reached the parent.
    expect(received).toHaveLength(1)
    const armed = received[0] as LegacyGoalLoop
    expect(armed.kind).toBe('legacy_goal_loop')
    expect(armed.message).toBe('edited, as the frame carries it')
    expect(Math.abs((armed.nextDueAt ?? 0) - Date.now() / 1000)).toBeLessThan(5)
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

  it('saves a non-target edit without parsing an unchanged malformed target', async () => {
    ;(api.monitorUpdate as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, monitor: {},
    })
    renderPopover({ ...activeMonitor, target: 'malformed persisted target' })

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Probe cadence in seconds' }), {
      target: { value: '600' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(api.monitorUpdate).toHaveBeenCalledWith('monitor-1', {
      cadence_secs: 600,
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
    expect(screen.queryByRole('button', { name: 'New monitor' })).not.toBeInTheDocument()
    expect(screen.getAllByText('budget stopped')).toHaveLength(2)
    expect(screen.getByText('token_budget')).toHaveClass('font-mono')
    expect(screen.getByText('Address actionable review feedback.')).toBeInTheDocument()
    expect(screen.getByText('250,000')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart monitor' }))
    await waitFor(() => expect(api.monitorRestart).toHaveBeenCalledWith('monitor-1'))
  })

  it('offers Clear beside Restart on a stopped monitor, behind a confirm', async () => {
    // Restart alone is not a way out: the stopped record keeps occupying the
    // session and a retained stop REFUSES a new monitor, so a user who wants to
    // watch a different pull request is stuck with the one they stopped
    // watching. Clearing removes the record, and it is irreversible, so it sits
    // behind the same confirm the stop control uses.
    ;(api.monitorClear as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, monitor: null })
    renderPopover({
      ...activeMonitor,
      active: false,
      terminal: { outcome: 'user_stop', reason: 'user_stop', stoppedAt: 1_800_000_100 },
    })

    // Both exits are named where the terminal state is described.
    expect(screen.getByTestId('monitor-terminal-exits').textContent).toContain('removes this monitor for good')
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped monitor' }))
    // One press does not erase anything.
    expect(api.monitorClear).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Restart monitor' })).toBeNull()
    // The exits line becomes the question rather than naming the buttons that
    // just left the row.
    expect(screen.getByTestId('monitor-terminal-exits').textContent)
      .toBe('Remove this monitor for good?')
    fireEvent.click(screen.getByRole('button', { name: 'Clear monitor for good' }))
    await waitFor(() => expect(api.monitorClear).toHaveBeenCalledWith('monitor-1'))
  })

  it('lets a confirm on the clear be cancelled without erasing anything', () => {
    renderPopover({
      ...activeMonitor,
      active: false,
      terminal: { outcome: 'user_stop', reason: 'user_stop', stoppedAt: 1_800_000_100 },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped monitor' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(api.monitorClear).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Restart monitor' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Clear stopped monitor' })).toBeTruthy()
  })

  it('drops a primed confirmation when the monitor changes under the popover', () => {
    // Same hazard as the legacy surface: this popover re-renders from websocket
    // state without closing, so another client can swap the record while a
    // confirmation is primed and the press would act on a record the
    // confirmation never described.
    const terminal = {
      ...activeMonitor,
      active: false,
      terminal: { outcome: 'user_stop', reason: 'user_stop', stoppedAt: 1_800_000_100 },
    } as StructuredMonitor
    const { rerenderAutomation } = renderPopover(terminal)

    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped monitor' }))
    expect(screen.getByRole('button', { name: 'Clear monitor for good' })).toBeTruthy()
    rerenderAutomation({ ...terminal, active: true, terminal: null })

    expect(screen.queryByRole('button', { name: 'Clear monitor for good' })).toBeNull()
    expect(api.monitorClear).not.toHaveBeenCalled()
  })

  it('offers no clear control while a monitor is still running', () => {
    // Clearing a LIVE watch would delete it with no record it existed, which is
    // what the server refuses; the surface must not offer the press either.
    renderPopover(activeMonitor)

    expect(screen.queryByRole('button', { name: 'Clear stopped monitor' })).toBeNull()
    expect(screen.queryByTestId('monitor-terminal-exits')).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop monitor' })).toBeTruthy()
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

  it('opens on the goal loop so a session with no pull request can still set a goal', () => {
    renderPopover(null, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Pull request URL' })).not.toBeInTheDocument()
    /* The composer button must name the surface it opens. It said "Set up a
       bounded monitor" while the monitor was the default, which promised a
       PR-only form to every session. */
    expect(screen.getByRole('button', { name: 'Set a goal' })).toBeInTheDocument()
  })

  it('opens on a live monitor rather than the default goal loop', () => {
    renderPopover(activeMonitor, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    expect(screen.getByText('https://github.com/kirodotdev/KiroCrew/pull/42')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Goal description' })).not.toBeInTheDocument()
  })

  it('offers the old costly loop explicitly without changing zero-unlimited semantics', () => {
    renderPopover(null, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    const notice = screen.getByText(
      'This goal loop invokes the agent every cycle and can run without a limit.',
    )
    const panel = notice.closest('[data-side]')
    const maxCycles = screen.getByRole('spinbutton', { name: 'Max cycles (0 = infinite)' })
    expect(notice).toBeInTheDocument()
    /* Warn-coloured, as it was when this form was opt-in. On the view every
       reader now lands on, this sentence is the only cost cue the surface
       carries, so muting it would have weakened that cue in the same change
       that made the surface the default. */
    expect(notice).toHaveClass('border-warn/30', 'bg-warn-subtle', 'text-warn-fg')
    expect(panel).toHaveClass(
      'w-[min(calc(100vw-1rem),26.25rem)]',
      'max-h-[min(80vh,42rem)]',
      'overflow-y-auto',
    )
    expect(maxCycles).toHaveValue(0)
    expect(maxCycles.parentElement?.parentElement).toHaveClass('flex-col', 'sm:flex-row')

    fireEvent.click(screen.getByRole('button', { name: 'Watch a pull request instead' }))
    expect(screen.getByRole('textbox', { name: 'Pull request URL' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Back to goal loop' }))
    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
  })

  it('reopens an unarmed slot on the default view after the bounded form was visited', () => {
    const onOpenChange = vi.fn()
    const { rerenderAutomation } = renderPopover(
      null, vi.fn(), true, onOpenChange, '', { enterBounded: false },
    )

    fireEvent.click(screen.getByRole('button', { name: 'Watch a pull request instead' }))
    expect(screen.getByRole('textbox', { name: 'Pull request URL' })).toBeInTheDocument()

    /* Close, then reopen the same unarmed slot. The view is re-derived from the
       record on every open, so the reader's earlier switch does not turn the
       pull-request form back into this slot's default. */
    rerenderAutomation(null, 'chat-1', false)
    rerenderAutomation(null, 'chat-1', true)

    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Pull request URL' })).not.toBeInTheDocument()
  })

  it('marks the only route to the monitor as a link without needing hover', () => {
    renderPopover(null, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    /* A hover-only affordance is invisible on a touch viewport, and this is now
       the sole path to the bounded form. */
    expect(screen.getByRole('button', { name: 'Watch a pull request instead' }))
      .toHaveClass('underline')
  })

  it('shows the goal glyph on the trigger while nothing is armed', () => {
    const { container, rerenderAutomation } = renderPopover(
      null, vi.fn(), true, vi.fn(), '', { enterBounded: false },
    )

    /* The glyph must promise what the button opens: with nothing armed it opens
       the goal editor, and there is no probing to depict. */
    expect(container.querySelector('.lucide-goal')).toBeTruthy()
    expect(container.querySelector('.lucide-radar')).toBeNull()

    rerenderAutomation(activeMonitor)
    expect(container.querySelector('.lucide-radar')).toBeTruthy()
  })

  it.each(['crew', 'member'])('says why the goal fields are dead in %s mode', sessionMode => {
    renderPopover(null, vi.fn(), true, vi.fn(), sessionMode, { enterBounded: false })

    /* The explanation used to sit on the bounded view because that was the
       default; a disabled form with no reason on it is what flipping the
       default would otherwise have produced. */
    expect(screen.getByTestId('auto-nudge-write-disabled-reason')).toHaveTextContent(
      "Automations aren't available in crew or member sessions because those sessions route work through their crew.",
    )
    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeDisabled()
  })

  it('offers no bounded monitor while a legacy loop is already running', () => {
    renderPopover(activeLegacyLoop, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Watch a pull request instead' })).not.toBeInTheDocument()
  })

  /* THE JUDGE LINE, mounted the way the dashboard mounts it.
     The row below is `GET /api/autonudge` output in the wire's own spelling, and it
     is parsed by the real normalizer rather than written as a record, because the
     three hops between the endpoint and the line each name their fields: the
     publisher's keys, this record's, and the adapter's. A test that hands the
     popover a loop object directly agrees with the reader about every name and
     still passes while a middle hop carries none of them -- and a dropped judge is
     silent on screen, because "this loop has no judge" is the honest reading for
     most loops and renders nothing. So the assertion has to start at the wire. */
  const judgeLoopRow = (judge: Record<string, unknown>) => ({
    id: 'legacy-judge', slot_key: 'chat-1', message: 'Keep checking.',
    idle_secs: 300, max_cycles: 24, cycle_count: 2, active: true,
    last_fire_ts: 1_800_000_000, next_due_ts: 1_900_000_000, stopped_reason: '',
    ...judge,
  })

  it('renders the judge line from a GET row, criterion and verdict both', () => {
    const record = normalizeAutomationRecord(judgeLoopRow({
      judge: { wake_when: 'a reviewer asks for changes', quiet_when: '', targets: [] },
      judge_last_verdict: { outcome: 'quiet', evidence_items: 2, at: 1_800_000_500 },
    }))
    renderPopover(record, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    const line = screen.getByTestId('judge-line')
    expect(line).toHaveTextContent('Judge: wake when a reviewer asks for changes')
    expect(line).toHaveTextContent('quiet')
    expect(line).toHaveTextContent('2 items')
  })

  it('renders the judge line with no verdict yet when the judge has not answered', () => {
    const record = normalizeAutomationRecord(judgeLoopRow({
      judge: { wake_when: '', quiet_when: 'the build is still running', targets: [] },
    }))
    renderPopover(record, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    // The LABEL as well as the criterion. A quiet-only brief under the wake label
    // states the inverse of what the owner armed, and an assertion on the criterion
    // alone passes either way, because the criterion travels either way.
    const line = screen.getByTestId('judge-line')
    expect(line).toHaveTextContent('Judge: stay quiet while the build is still running')
    expect(line).not.toHaveTextContent('wake when')
    expect(line).toHaveTextContent('no verdict yet')
  })

  it('renders a verdict with no timestamp without a dangling separator', () => {
    const record = normalizeAutomationRecord(judgeLoopRow({
      judge: { wake_when: 'a reviewer asks for changes', quiet_when: '', targets: [] },
      judge_last_verdict: { outcome: 'quiet', evidence_items: 2, at: 0 },
    }))
    renderPopover(record, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    const line = screen.getByTestId('judge-line')
    expect(line).toHaveTextContent('2 items')
    expect(line.textContent?.trimEnd().endsWith('·')).toBe(false)
  })

  it('carries a criterion at the arming bound in full, styled as its sibling rows', () => {
    // The arming surface refuses anything past MAX_JUDGE_CRITERION_CHARS (500), so a
    // criterion this long is the widest the render can ever be handed. It is shown
    // whole rather than clipped: the owner reads back exactly the prose they armed,
    // and the row carries its siblings' type contract so a long brief grows the
    // popover the way every other wrapping row in it does.
    const criterion = 'w'.repeat(500)
    const record = normalizeAutomationRecord(judgeLoopRow({
      judge: { wake_when: criterion, quiet_when: '', targets: [] },
      judge_last_verdict: { outcome: 'quiet', evidence_items: 1, at: 1_800_000_500 },
    }))
    renderPopover(record, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    const line = screen.getByTestId('judge-line')
    expect(line.textContent).toContain(criterion)
    expect(line).toHaveTextContent('quiet')
    expect(line.className).toContain('text-[11px]')
    // This criterion is 500 characters with NO space in it, which is the input that
    // makes the difference between wrapping and overflowing: without a break rule the
    // row runs off the popover horizontally instead of growing it. A rendered capture
    // of this exact case is attached to the pull request.
    expect(line.className).toContain('break-words')
  })

  it('draws no judge line for a loop whose row carries a cleared brief', () => {
    const record = normalizeAutomationRecord(judgeLoopRow({ judge: {} }))
    renderPopover(record, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
    expect(screen.queryByTestId('judge-line')).not.toBeInTheDocument()
  })

  it('keeps a malformed judge inert rather than throwing inside the render', () => {
    const record = normalizeAutomationRecord(judgeLoopRow({
      judge: { wake_when: 42, quiet_when: null, targets: ['ok', 7] },
      judge_last_verdict: { outcome: {}, evidence_items: -1, at: 'now' },
    }))
    renderPopover(record, vi.fn(), true, vi.fn(), '', { enterBounded: false })

    expect(screen.getByRole('textbox', { name: 'Goal description' })).toBeInTheDocument()
    expect(screen.queryByTestId('judge-line')).not.toBeInTheDocument()
  })

  it('falls back to the shipped ceiling when a refetch fails after a raised ceiling', async () => {
    ;(api.monitorForSlot as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({
        enabled: true, monitor: null, max_runtime_ceiling_secs: 2_592_000,
      })
      .mockRejectedValueOnce(new Error('offline'))
    const { client } = renderPopover(null)
    const runtime = screen.getByRole('spinbutton', { name: 'Maximum runtime in seconds' })
    await waitFor(() => expect(runtime).toHaveAttribute('max', '2592000'))

    await act(async () => {
      await client.refetchQueries({ queryKey: ['monitor-runtime-ceiling', 'chat-1'] })
    })

    expect(await screen.findByTestId('monitor-read-error')).toHaveAttribute('role', 'alert')
    expect(runtime).toHaveAttribute('max', '604800')
  })
})
