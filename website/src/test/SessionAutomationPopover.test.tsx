import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
    monitorCreate: vi.fn(),
    monitorUpdate: vi.fn(),
    monitorStop: vi.fn(),
    monitorClear: vi.fn(),
    monitorRestart: vi.fn(),
    stopChatSlot: vi.fn(),
    autonudgeForSlot: vi.fn(),
    autonudgeResume: vi.fn(),
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
    vi.mocked(api.stopChatSlot).mockReset()
    vi.mocked(api.autonudgeForSlot).mockReset()
    vi.mocked(api.autonudgeResume).mockReset()
    framerMocks.reducedMotion = false
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows an automatically pursued goal with progress, criteria, and a visible status', () => {
    renderPopover({
      ...activeLegacyLoop,
      goal: {
        objective: 'Add keyboard navigation',
        criteria: ['Arrow keys move focus'],
        progress: 'Verifying focus behavior',
        status: 'working',
        evidence: [],
      },
    })
    expect(screen.getByRole('button', { name: 'Working toward your goal: Add keyboard navigation' })).toBeInTheDocument()
    expect(screen.getByText('Arrow keys move focus')).toBeInTheDocument()
    expect(screen.getByText('Verifying focus behavior')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it.each(['chat-1', 'slack:123.456'])('pauses the visible session when its goal is bound to %s', async binding => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop,
      slotKey: binding,
      goal: {
        objective: 'Build the feature', criteria: ['Tests pass'],
        progress: '', status: 'working', evidence: [],
      },
    }
    vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
    vi.mocked(api.autonudgeForSlot).mockResolvedValue({
      enabled: true,
      loop: {
        id: loop.id, slot_key: loop.slotKey, active: false,
        goal: { ...loop.goal, status: 'paused' },
      },
    })
    const { onChange } = renderPopover(loop)
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(api.stopChatSlot).toHaveBeenCalledWith('chat-1'))
    await waitFor(() => expect(api.autonudgeForSlot).toHaveBeenCalledWith(binding))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      active: false, goal: expect.objectContaining({ status: 'paused' }),
    })))
  })

  it('keeps completion evidence visible and removes the resume action', () => {
    renderPopover({
      ...activeLegacyLoop, active: false, stoppedReason: 'goal_complete',
      goal: {
        objective: 'Build the feature', criteria: ['Tests pass'], progress: 'Delivered',
        status: 'complete', evidence: ['Keyboard tests pass'],
      },
    })
    expect(screen.getByText('Keyboard tests pass')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Goal achieved: Build the feature' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
  })

  it.each([
    ['complete', 'goal_complete', undefined],
    ['ended', 'goal_ended', undefined],
    ['complete', 'goal_complete', 3],
    ['ended', 'goal_ended', 3],
    ['paused', 'manual', 3],
    ['paused', 'goal_pause_unsaved', 3],
  ] as const)('preserves %s (%s, generation %s) before a Resume response arrives', async (status, reason, generation) => {
    const paused: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: 'manual', goalGeneration: 1,
      goal: {
        objective: 'Build the feature', criteria: ['Tests pass'], progress: '',
        status: 'paused', evidence: [],
      },
    }
    let resolveResume!: (value: { loop: unknown }) => void
    const pendingResume = new Promise<{ loop: unknown }>(resolve => { resolveResume = resolve })
    vi.mocked(api.autonudgeResume).mockReturnValue(pendingResume)
    const { client, onChange, rerenderAutomation } = renderPopover(paused)
    const queryKey = ['session-automation', 'chat-1']
    client.setQueryData(queryKey, paused)
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(paused.id, 1))

    const newer = normalizeAutomationRecord({
      id: paused.id, slot_key: paused.slotKey, active: false,
      stopped_reason: reason, generation,
      goal: { ...paused.goal!, status, evidence: ['Keyboard tests pass'] },
    }) as LegacyGoalLoop
    // The WebSocket handler publishes this before the older HTTP response lands.
    client.setQueryData(queryKey, newer)
    rerenderAutomation(newer)
    expect(screen.getByText('Keyboard tests pass')).toBeInTheDocument()
    await act(async () => {
      resolveResume({
        loop: {
          id: paused.id, slot_key: paused.slotKey, active: true,
          config_generation: 2,
          goal: { ...paused.goal, status: 'working' },
        },
      })
      await pendingResume
    })
    // onSettled runs after onSuccess; wait for it before checking for stale publication.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey }))
    expect(onChange).not.toHaveBeenCalled()
    expect(client.getQueryData(queryKey)).toEqual(newer)
    if (reason === 'manual') {
      expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled()
    } else if (reason === 'goal_pause_unsaved') {
      expect(screen.getByRole('button', { name: 'Retry saving pause' })).toBeEnabled()
    } else {
      expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
    }
  })

  it.each([false, true])('accepts a current Resume response (already observed: %s)', async observed => {
    const wire = {
      id: activeLegacyLoop.id, slot_key: activeLegacyLoop.slotKey,
      active: false, config_generation: 1, stopped_reason: 'manual',
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'paused', evidence: [] },
    }
    const paused = normalizeAutomationRecord(wire) as LegacyGoalLoop
    const resumed = { ...wire, active: true, config_generation: 3, stopped_reason: '', goal: { ...wire.goal, status: 'working' } }
    let resolveResume!: (value: { loop: unknown }) => void
    const pendingResume = new Promise<{ loop: unknown }>(resolve => { resolveResume = resolve })
    vi.mocked(api.autonudgeResume).mockReturnValue(pendingResume)
    const { client, onChange, rerenderAutomation } = renderPopover(paused)
    const queryKey = ['session-automation', 'chat-1']
    client.setQueryData(queryKey, paused)
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(paused.id, 1))
    if (observed) {
      const current = normalizeAutomationRecord(resumed)
      client.setQueryData(queryKey, current)
      rerenderAutomation(current)
    }
    await act(async () => {
      resolveResume({ loop: resumed })
      await pendingResume
    })
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(normalizeAutomationRecord(resumed)))
  })

  it.each(['replaced', 'removed'] as const)('preserves a %s goal after the Resume control unmounts', async change => {
    const paused: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: 'manual', goalGeneration: 1,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'paused', evidence: [] },
    }
    let resolveResume!: (value: { loop: unknown }) => void
    const pendingResume = new Promise<{ loop: unknown }>(resolve => { resolveResume = resolve })
    vi.mocked(api.autonudgeResume).mockReturnValue(pendingResume)
    const { client, onChange, unmount } = renderPopover(paused)
    const queryKey = ['session-automation', 'chat-1']
    client.setQueryData(queryKey, paused)
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(paused.id, 1))
    unmount()
    const current = change === 'replaced' ? { ...paused, id: 'replacement-goal' } : null
    client.setQueryData(queryKey, current)
    await act(async () => {
      resolveResume({
        loop: {
          id: paused.id, slot_key: paused.slotKey, active: true,
          config_generation: 2, goal: { ...paused.goal, status: 'working' },
        },
      })
      await pendingResume
    })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey }))
    expect(onChange).not.toHaveBeenCalled()
    expect(client.getQueryData(queryKey)).toEqual(current)
  })

  it('directs required information to chat without submitting a resume request', () => {
    renderPopover({
      ...activeLegacyLoop, active: false, stoppedReason: 'goal_needs_input',
      goal: {
        objective: 'Build the feature', criteria: [], progress: 'Which repository should change?',
        status: 'needs_input', evidence: [],
      },
    })
    expect(screen.getByText('Reply in chat with the requested information.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^(Pause|Resume|Retry)$/ })).not.toBeInTheDocument()
    expect(api.autonudgeResume).not.toHaveBeenCalled()
  })

  it.each([
    ['cycle_cap', 1250, 14400, 'Automatic turn limit reached', 'automatic turn limit (1,250 / 1,250)'],
    ['cycle_cap', 0, 14400, 'Automatic turn limit reached', 'automatic turn limit.'],
    ['runtime_budget', 50, 14400, 'Time limit reached', 'elapsed-time limit (4 hours)'],
    ['runtime_budget', 50, 3661, 'Time limit reached', 'elapsed-time limit (1 hour 1 minute 1 second)'],
    ['runtime_budget', 50, undefined, 'Time limit reached', 'elapsed-time limit.'],
  ] as const)('explains exhausted %s bounds (%s/%s) without offering Resume', (reason, maxCycles, maxRuntimeSecs, label, explanation) => {
    renderPopover({
      ...activeLegacyLoop, active: false, stoppedReason: reason,
      maxCycles, cycleCount: maxCycles, maxRuntimeSecs,
      goal: { objective: 'Build the feature', criteria: [], progress: 'Tests remain', status: 'paused', evidence: [] },
    })
    const trigger = screen.getByRole('button', { name: `${label}: Build the feature` })
    expect(within(trigger).getByRole('status')).toHaveTextContent(label)
    expect(within(screen.getByRole('dialog', { name: 'Build the feature' })).getByText(label)).toBeVisible()
    expect(screen.getByText(content => content.includes(explanation))).toHaveTextContent(
      'Review progress, then ask in chat to end this goal and start a new one for the remaining work.',
    )
    expect(screen.queryByRole('button', { name: /^(Pause|Resume|Retry)$/ })).not.toBeInTheDocument()
    expect(api.autonudgeResume).not.toHaveBeenCalled()
  })

  it('resumes work after an approval stall with the captured revision', async () => {
    vi.mocked(api.autonudgeResume).mockResolvedValue({ loop: null })
    renderPopover({
      ...activeLegacyLoop, active: false, stoppedReason: 'approval_stalled', goalGeneration: 0,
      goal: {
        objective: 'Build the feature', criteria: ['Tests pass'], progress: '',
        status: 'paused', evidence: [],
      },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(activeLegacyLoop.id, 0))
    expect(api.stopChatSlot).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Approval timed out: Build the feature' })).toBeInTheDocument()
  })

  it.each([
    [true, '', 'Pause', 'The pause request failed. Please try again.'],
    [false, 'manual', 'Resume', 'The resume request failed. Please try again.'],
    [false, 'approval_stalled', 'Resume', 'The resume request failed. Please try again.'],
    [false, 'goal_pause_unsaved', 'Retry saving pause', 'The request to save the pause failed. Please try again.'],
  ] as const)('attributes a failed %s/%s request to the captured action after live state changes', async (active, stoppedReason, action, message) => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active, stoppedReason, goalGeneration: 1,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: active ? 'working' : 'paused', evidence: [] },
    }
    let rejectWrite!: (error: Error) => void
    const pending = new Promise<never>((_resolve, reject) => { rejectWrite = reject })
    const pauseRequest = active || stoppedReason === 'goal_pause_unsaved'
    if (pauseRequest) vi.mocked(api.stopChatSlot).mockReturnValue(pending)
    else vi.mocked(api.autonudgeResume).mockReturnValue(pending)
    const { rerenderAutomation, onChange } = renderPopover(loop)
    fireEvent.click(screen.getByRole('button', { name: action }))
    await waitFor(() => expect(pauseRequest ? api.stopChatSlot : api.autonudgeResume).toHaveBeenCalled())
    const newer: LegacyGoalLoop = {
      ...loop, active: !active, stoppedReason: active ? 'manual' : '',
      goal: { ...loop.goal!, status: active ? 'paused' : 'working' },
    }
    rerenderAutomation(newer)
    await act(async () => { rejectWrite(new Error('offline')); await pending.catch(() => {}) })
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(api.autonudgeForSlot).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    rerenderAutomation({ ...newer, id: 'another-goal' })
    expect(screen.queryByText(message)).not.toBeInTheDocument()
  })

  it.each(['', 'goal_pause_unsaved'])('distinguishes a failed refresh after an accepted pause (%s)', async stoppedReason => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active: !stoppedReason, stoppedReason,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: stoppedReason ? 'paused' : 'working', evidence: [] },
    }
    vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
    vi.mocked(api.autonudgeForSlot).mockRejectedValue(new Error('refresh unavailable'))
    const { onChange } = renderPopover(loop)
    fireEvent.click(screen.getByRole('button', { name: stoppedReason ? 'Retry saving pause' : 'Pause' }))
    expect(await screen.findByText("The pause request was received, but the goal's status could not be refreshed.")).toBeInTheDocument()
    expect(api.stopChatSlot).toHaveBeenCalledWith('chat-1')
    expect(api.autonudgeForSlot).toHaveBeenCalledWith(loop.slotKey)
    expect(api.autonudgeResume).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Pause requested — status unconfirmed: Build the feature' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^(Pause|Resume|Retry saving pause)$/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('deduplicates a save-retry failure while retaining current pause facts and the captured failure', async () => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: 'goal_pause_unsaved', goalGeneration: 2,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'paused', evidence: [] },
    }
    let rejectWrite!: (error: Error) => void
    const pending = new Promise<never>((_resolve, reject) => { rejectWrite = reject })
    vi.mocked(api.stopChatSlot).mockReturnValue(pending)
    const { rerenderAutomation } = renderPopover(loop)
    fireEvent.click(screen.getByRole('button', { name: 'Retry saving pause' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry saving pause' })).toBeDisabled())
    await act(async () => { rejectWrite(new Error('offline')); await pending.catch(() => {}) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry saving pause' })).toBeEnabled())
    const notice = screen.getByRole('alert')
    expect(api.stopChatSlot).toHaveBeenCalledWith(loop.slotKey)
    expect(api.stopChatSlot).toHaveBeenCalledTimes(1)
    expect(notice).not.toHaveTextContent('The request to save the pause failed.')
    expect(notice).toHaveTextContent('Work is paused now')
    expect(notice).toHaveTextContent('saving the pause failed')
    expect(notice).toHaveTextContent('pause may be lost and work may resume automatically')
    expect(within(notice).getAllByRole('button', { name: 'Ask the agent' })).toHaveLength(1)
    rerenderAutomation({
      ...loop, active: true, stoppedReason: '', goalGeneration: 4,
      goal: { ...loop.goal!, status: 'working' },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('The request to save the pause failed.')
    expect(screen.getByRole('alert')).not.toHaveTextContent('Work is paused now')
  })

  it.each(['manual', 'approval_stalled'])('reads a missing revision before a separate %s resume click', async reason => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: reason,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'paused', evidence: [] },
    }
    vi.mocked(api.autonudgeForSlot).mockResolvedValue({
      enabled: true,
      loop: { id: loop.id, slot_key: loop.slotKey, active: false, stopped_reason: reason, config_generation: 0, goal: loop.goal },
    })
    vi.mocked(api.autonudgeResume).mockRejectedValue(new ApiError(409, 'Conflict', '{"code":"goal_generation_conflict"}'))
    const { onChange, rerenderAutomation } = renderPopover(loop)
    expect(screen.queryByRole('button', { name: /^(Resume|Retry)$/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ goalGeneration: 0 })))
    expect(api.autonudgeResume).not.toHaveBeenCalled()
    rerenderAutomation(onChange.mock.calls[0][0])
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    // The wire fence must preserve zero and must never retry a 409 with a fresh revision.
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(loop.id, 0))
    expect(await screen.findByText('The resume request failed. Please try again.')).toBeInTheDocument()
    expect(api.autonudgeResume).toHaveBeenCalledTimes(1)
    expect(api.autonudgeForSlot).toHaveBeenCalledTimes(1)
  })

  it.each(['manual', 'approval_stalled'])('recovers a failed missing-revision %s refresh with GET before a separate Resume', async reason => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: reason,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'paused', evidence: [] },
    }
    vi.mocked(api.autonudgeForSlot).mockRejectedValueOnce(new Error('offline'))
    vi.mocked(api.autonudgeResume).mockResolvedValue({ loop: null })
    const { onChange, rerenderAutomation } = renderPopover(loop)
    expect(screen.getByText('Refresh to confirm this goal’s current status before resuming.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    expect(await screen.findByText('The goal status could not be refreshed.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^(Resume|Retry)$/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Ask the agent' })).toHaveLength(1)
    expect(api.autonudgeForSlot).toHaveBeenCalledWith(loop.slotKey)
    expect(api.autonudgeForSlot).toHaveBeenCalledTimes(1)
    expect(api.stopChatSlot).not.toHaveBeenCalled()
    expect(api.autonudgeResume).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()

    vi.mocked(api.autonudgeForSlot).mockResolvedValue({
      enabled: true,
      loop: { id: loop.id, slot_key: loop.slotKey, active: false, stopped_reason: reason, config_generation: 0, goal: loop.goal },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ goalGeneration: 0 })))
    expect(api.autonudgeForSlot).toHaveBeenCalledTimes(2)
    expect(api.stopChatSlot).not.toHaveBeenCalled()
    expect(api.autonudgeResume).not.toHaveBeenCalled()
    rerenderAutomation(onChange.mock.calls[0][0])
    expect(screen.queryByText('Refresh to confirm this goal’s current status before resuming.')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(loop.id, 0))
    expect(api.autonudgeResume).toHaveBeenCalledTimes(1)
  })

  it('clarifies blocked recovery and resumes with the revision observed at the click', async () => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: 'goal_blocked', goalGeneration: 7,
      goal: { objective: 'Build the feature', criteria: [], progress: 'A dependency is missing', status: 'blocked', evidence: [] },
    }
    vi.mocked(api.autonudgeResume).mockResolvedValue({ loop: null })
    renderPopover(loop)
    expect(screen.getByText('Resolve the blocker or change the plan in chat, then select Resume to try this goal again.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(loop.id, 7))
  })

  it('refreshes an unconfirmed pause with GET only and reconciles the active projection', async () => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, goalGeneration: 1,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'working', evidence: [] },
    }
    vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
    vi.mocked(api.autonudgeForSlot).mockRejectedValueOnce(new Error('offline'))
    const { client, onChange, rerenderAutomation } = renderPopover(loop)
    client.setQueryData(['session-automation', 'chat-1'], loop)
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await screen.findByText("The pause request was received, but the goal's status could not be refreshed.")
    // Closing the popup must not lose the uncertainty displayed in the composer.
    rerenderAutomation(loop, 'chat-1', false)
    expect(screen.getByRole('button', { name: 'Pause requested — status unconfirmed: Build the feature' })).toBeInTheDocument()
    rerenderAutomation(loop)
    vi.mocked(api.autonudgeForSlot).mockRejectedValueOnce(new Error('still offline'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await waitFor(() => expect(api.autonudgeForSlot).toHaveBeenCalledTimes(2))
    expect(await screen.findByText("The pause request was received, but the goal's status could not be refreshed.")).toBeInTheDocument()
    let resolveRead!: (value: { enabled: boolean; loop: unknown }) => void
    vi.mocked(api.autonudgeForSlot).mockReturnValue(new Promise(resolve => { resolveRead = resolve }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await waitFor(() => expect(api.autonudgeForSlot).toHaveBeenCalledTimes(3))
    expect(screen.getByRole('button', { name: 'Pause requested — status unconfirmed: Build the feature' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeDisabled()
    await act(async () => resolveRead({
      enabled: true,
      loop: { id: loop.id, slot_key: loop.slotKey, active: false, config_generation: 2, stopped_reason: 'manual', goal: { ...loop.goal, status: 'paused' } },
    }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      active: false, goalGeneration: 2, goal: expect.objectContaining({ status: 'paused' }),
    })))
    rerenderAutomation(onChange.mock.calls[0][0])
    expect(screen.getByRole('button', { name: 'Goal paused: Build the feature' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(api.stopChatSlot).toHaveBeenCalledTimes(1)
    expect(api.autonudgeResume).not.toHaveBeenCalled()
  })

  it.each(['working', 'paused'] as const)('keeps pause uncertainty when reconnect left an older %s cache snapshot', async cachedStatus => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, goalGeneration: 2,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'working', evidence: [] },
    }
    vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
    vi.mocked(api.autonudgeForSlot).mockRejectedValue(new Error('offline'))
    const { client, onChange } = renderPopover(loop)
    // Reconnect seeds active Redux records without advancing the per-slot cache.
    client.setQueryData(['session-automation', 'chat-1'], {
      ...loop, goalGeneration: 1, active: cachedStatus === 'working',
      stoppedReason: cachedStatus === 'paused' ? 'manual' : '',
      goal: { ...loop.goal!, status: cachedStatus },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await screen.findByText("The pause request was received, but the goal's status could not be refreshed.")
    expect(screen.getByRole('button', { name: 'Pause requested — status unconfirmed: Build the feature' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await waitFor(() => expect(api.autonudgeForSlot).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh status' })).toBeEnabled())
    expect(screen.getByRole('alert')).toHaveTextContent("The pause request was received")
    expect(api.stopChatSlot).toHaveBeenCalledTimes(1)
    expect(api.autonudgeResume).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()

    await act(async () => {
      client.setQueryData(['session-automation', 'chat-1'], {
        ...loop, goalGeneration: 3, active: false, stoppedReason: 'goal_complete',
        goal: { ...loop.goal!, status: 'complete', evidence: ['Tests passed'] },
      })
    })
    await screen.findByRole('button', { name: 'Goal achieved: Build the feature' })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Refresh status' })).not.toBeInTheDocument()
  })

  it.each(['paused', 'complete'] as const)('uses a background-confirmed %s revision while the active projection lags', async status => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, goalGeneration: 1,
      goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'working', evidence: [] },
    }
    vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
    vi.mocked(api.autonudgeForSlot).mockRejectedValue(new Error('offline'))
    const { client, onChange } = renderPopover(loop)
    client.setQueryData(['session-automation', 'chat-1'], loop)
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await screen.findByText("The pause request was received, but the goal's status could not be refreshed.")
    const confirmed: LegacyGoalLoop = {
      ...loop, active: false, stoppedReason: status === 'paused' ? 'manual' : 'goal_complete',
      goalGeneration: 2, goal: { ...loop.goal!, status, evidence: status === 'complete' ? ['Tests passed'] : [] },
    }
    // ChatPage's active Redux projection still supplies the original prop.
    // A background per-slot GET advances only the existing query cache.
    await act(async () => { client.setQueryData(['session-automation', 'chat-1'], confirmed) })
    const label = status === 'paused' ? 'Goal paused' : 'Goal achieved'
    await screen.findByRole('button', { name: `${label}: Build the feature` })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^(Pause|Refresh status)$/ })).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
    if (status === 'paused') {
      vi.mocked(api.autonudgeResume).mockResolvedValue({ loop: null })
      fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
      await waitFor(() => expect(api.autonudgeResume).toHaveBeenCalledWith(loop.id, 2))
    } else {
      expect(screen.getByText('Tests passed')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
    }
  })

  it.each(['resume', 'pause', 'complete', 'ended', 'replacement', 'removed', 'session', 'binding', 'cache'] as const)(
    'a late pause refresh failure cannot mask newer %s truth', async change => {
      const loop: LegacyGoalLoop = {
        ...activeLegacyLoop, goalGeneration: 1,
        goal: { objective: 'Build the feature', criteria: [], progress: '', status: 'working', evidence: [] },
      }
      vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
      let rejectRead!: (error: Error) => void
      const pending = new Promise<never>((_resolve, reject) => { rejectRead = reject })
      vi.mocked(api.autonudgeForSlot).mockReturnValue(pending)
      const { client, rerenderAutomation, onChange } = renderPopover(loop)
      client.setQueryData(['session-automation', 'chat-1'], loop)
      const invalidate = vi.spyOn(client, 'invalidateQueries')
      fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
      await waitFor(() => expect(api.autonudgeForSlot).toHaveBeenCalled())
      const newer: LegacyGoalLoop = {
        ...loop, goalGeneration: 3,
        active: change === 'resume' || change === 'cache',
        stoppedReason: change === 'pause' ? 'manual' : '',
        goal: { ...loop.goal!, status: change === 'complete' || change === 'ended' ? change : change === 'pause' ? 'paused' : 'working' },
      }
      if (change === 'replacement') newer.id = 'new-goal'
      if (change === 'binding') newer.slotKey = 'slack:new-binding'
      if (change === 'session') rerenderAutomation(loop, 'chat-2')
      else if (change === 'cache') {
        await act(async () => { client.setQueryData(['session-automation', 'chat-1'], newer) })
      } else {
        client.setQueryData(['session-automation', 'chat-1'], change === 'removed' ? null : newer)
        rerenderAutomation(change === 'removed' ? null : newer)
      }
      await act(async () => { rejectRead(new Error('offline')); await pending.catch(() => {}) })
      await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-automation', 'chat-1'] }))
      expect(screen.queryByText("The pause request was received, but the goal's status could not be refreshed.")).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /^Pause requested/ })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Refresh status' })).not.toBeInTheDocument()
      expect(onChange).not.toHaveBeenCalled()
    },
  )

  it('discloses an unsaved pause and retries saving without resuming work', async () => {
    const loop: LegacyGoalLoop = {
      ...activeLegacyLoop, active: false, stoppedReason: 'goal_pause_unsaved',
      goal: {
        objective: 'Build the feature', criteria: ['Tests pass'], progress: '',
        status: 'paused', evidence: [],
      },
    }
    vi.mocked(api.stopChatSlot).mockResolvedValue({ ok: true })
    vi.mocked(api.autonudgeForSlot).mockResolvedValue({ enabled: true, loop: null })
    renderPopover(loop)
    expect(screen.getByText(/Work is paused now.*pause may be lost and work may resume automatically/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry saving pause' }))
    await waitFor(() => expect(api.stopChatSlot).toHaveBeenCalledWith('chat-1'))
    expect(api.autonudgeResume).not.toHaveBeenCalled()
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
    'keeps legacy Stop available but disables legacy writes in %s mode',
    sessionMode => {
      const fetchMock = vi.fn()
      vi.stubGlobal('fetch', fetchMock)
      renderPopover(activeLegacyLoop, vi.fn(), true, vi.fn(), sessionMode)

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

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/autonudge/legacy-1', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: 'Keep checking.', idle_secs: 300, max_cycles: 24, active: true,
      }),
    }))
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
})
