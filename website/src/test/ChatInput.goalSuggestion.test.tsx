import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import { SlotProvider } from '../providers/SlotContext'
import { loadGoalDraft, saveGoalDraft } from '../utils/goalDrafts'
import type { LegacyGoalLoop, StructuredMonitor } from '../monitoring/automation'

/* The Autonomous goal card inside the full-chat composer: Review & start opens
 * the existing goal editor, and every blocked state says what the session's
 * automation is doing and offers Open goal, the composer's own goal control. */

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
  sessionStorage.clear()
})

const goalLoop = (over: Partial<LegacyGoalLoop> = {}): LegacyGoalLoop => ({
  kind: 'legacy_goal_loop', id: 'loop-1', slotKey: 'chat-goal-card', message: 'Keep checking.',
  idleSecs: 300, maxCycles: 24, cycleCount: 2, active: true, lastFireAt: 0,
  stoppedReason: '', monitorOutcome: '', monitorKind: '',
  ...over,
})

const monitor = (): StructuredMonitor => ({
  kind: 'structured_monitor', id: 'monitor-1', slotKey: 'chat-goal-card', active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', cadenceSecs: 300,
  nextProbeAt: 1_800_000_300, wakeInstructions: 'Address actionable review feedback.',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'pending', reasonCode: 'checks_pending', observedAt: 1_800_000_000, decision: 'no_change' },
  usage: { probes: 5, wakes: 2, agentTurns: 2, inputTokens: 1200, outputTokens: 300, providerErrors: 1, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
})

const GOAL_RUNNING = 'A goal is running in this session. To start this one instead, stop it in Open goal.'
const GOAL_PAUSED = "This session's goal is paused, waiting on your reply. To start this one instead, stop it in Open goal."

describe('ChatInput autonomous goal card', () => {
  /** A composer that owns the automation popover's open state, the way the
   *  page does, so a click on the goal card can be followed into the popover. */
  function GoalSuggestionHarness({ automation = null, sessionMode, options = ['Show details'], isRunning = false, onOpenChange }: {
    automation?: LegacyGoalLoop | StructuredMonitor | null
    onOpenChange?: (open: boolean) => void
    sessionMode?: string
    options?: string[]
    isRunning?: boolean
  }) {
    const [open, setOpen] = React.useState(false)
    return (
      <SlotProvider slotId="chat-goal-card">
        <ChatInput
          {...defaultProps}
          isRunning={isRunning}
          followUpGoal="Deliver the recommended next actions"
          followUpOptions={options}
          followUpPicked={new Set()}
          onFollowUpSelect={() => {}}
          onAutomationClick={v => { onOpenChange?.(v); setOpen(v) }}
          automation={automation}
          automationOpen={open}
          onAutomationChange={() => {}}
          sessionMode={sessionMode}
          typedCommandMenus={false}
          promptOptimizer={false}
          slotApprovalChrome={false}
        />
      </SlotProvider>
    )
  }

  const stubAutomationFetch = () => vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ loop: null }),
  })) as unknown as typeof fetch)

  it('opens the existing Set-a-goal flow with the autonomous objective prefilled', async () => {
    stubAutomationFetch()
    saveGoalDraft('chat-goal-card', { message: 'Older goal', idleSecs: 300, maxCycles: 8 })
    try {
      renderWithProviders(<GoalSuggestionHarness />)
      expect(screen.queryByRole('button', { name: 'Open goal' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Review & start' }))
      // SessionAutomationPopover is a lazy import; cold chunk resolution may exceed the default wait.
      const goal = await screen.findByLabelText('Goal description', {}, { timeout: 5_000 }) as HTMLTextAreaElement
      await waitFor(() => expect(goal.value).toBe('Deliver the recommended next actions'))
      const [idle, cycles] = screen.getAllByRole('spinbutton') as HTMLInputElement[]
      expect(idle.value).toBe('300')
      expect(cycles.value).toBe('8')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('turns Review & start off after a storage failure and leaves Open goal as the one way on', async () => {
    stubAutomationFetch()
    const priorDraft = { message: 'Older goal', idleSecs: 300, maxCycles: 8 }
    saveGoalDraft('chat-goal-card', priorDraft)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('storage disabled', 'SecurityError')
    })
    try {
      renderWithProviders(<GoalSuggestionHarness />)
      fireEvent.click(screen.getByRole('button', { name: 'Review & start' }))

      expect(screen.queryByLabelText('Goal description')).not.toBeInTheDocument()
      expect(screen.getByTestId('goal-save-error')).toHaveTextContent(
        "Couldn't save this goal in the browser. Select the goal text above, then paste it in Open goal.",
      )
      expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
      expect(screen.queryByRole('button', { name: 'Copy goal' })).not.toBeInTheDocument()
      expect(loadGoalDraft('chat-goal-card')).toEqual(priorDraft)

      fireEvent.click(screen.getByRole('button', { name: 'Open goal' }))
      expect(await screen.findByLabelText('Goal description', {}, { timeout: 5_000 })).toBeInTheDocument()
    } finally {
      setItem.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('says a goal is running while its turn runs, and offers Open goal', () => {
    renderWithProviders(<GoalSuggestionHarness automation={goalLoop()} isRunning />)
    expect(screen.getByTestId('autonomous-goal-card')).toHaveTextContent(GOAL_RUNNING)
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Open goal' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Show details' })).toBeEnabled()
  })

  it('says a goal is running when the newest reply does not ask the user to choose', () => {
    renderWithProviders(<GoalSuggestionHarness automation={goalLoop()} options={[]} />)
    expect(screen.getByTestId('autonomous-goal-card')).toHaveTextContent(GOAL_RUNNING)
  })

  it('says the goal is paused when it waits on the reply options, as the sidebar row does', () => {
    renderWithProviders(<GoalSuggestionHarness automation={goalLoop()} />)
    const card = screen.getByTestId('autonomous-goal-card')
    expect(card).toHaveTextContent(GOAL_PAUSED)
    expect(card).not.toHaveTextContent('is running')
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
  })

  it('opens the composer goal control from a blocked card', () => {
    const onOpenChange = vi.fn()
    renderWithProviders(<GoalSuggestionHarness automation={goalLoop()} onOpenChange={onOpenChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open goal' }))
    expect(onOpenChange).toHaveBeenCalledWith(true)
  })

  it('uses monitor copy, running and paused, when a structured monitor blocks the suggestion', () => {
    const { unmount } = renderWithProviders(<GoalSuggestionHarness automation={monitor()} isRunning />)
    expect(screen.getByTestId('autonomous-goal-card')).toHaveTextContent(
      'A monitor is running in this session. To start this goal, stop it in Open goal.',
    )
    unmount()
    renderWithProviders(<GoalSuggestionHarness automation={monitor()} />)
    expect(screen.getByTestId('autonomous-goal-card')).toHaveTextContent(
      "This session's monitor is paused, waiting on your reply. To start this goal, stop it in Open goal.",
    )
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
  })

  it('distinguishes a retained stopped record from active automation', () => {
    renderWithProviders(<GoalSuggestionHarness automation={goalLoop({ active: false, stoppedReason: 'user stopped' })} />)
    expect(screen.getByTestId('autonomous-goal-card')).toHaveTextContent(
      'A stopped goal or monitor is still saved here. To start this goal, clear it in Open goal.',
    )
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Open goal' })).toBeEnabled()
  })

  it('describes both actions with the status line', () => {
    renderWithProviders(<GoalSuggestionHarness automation={goalLoop()} isRunning />)
    expect(screen.getByRole('button', { name: 'Review & start' })).toHaveAccessibleDescription(GOAL_RUNNING)
    expect(screen.getByRole('button', { name: 'Open goal' })).toHaveAccessibleDescription(GOAL_RUNNING)
  })

  it('keeps the card inert in a crew or member session, where there is nothing to open', () => {
    renderWithProviders(<GoalSuggestionHarness sessionMode="crew" />)
    const card = screen.getByTestId('autonomous-goal-card')
    expect(card).toHaveTextContent("Automations aren't available in crew or member sessions")
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Open goal' })).not.toBeInTheDocument()
  })
})
