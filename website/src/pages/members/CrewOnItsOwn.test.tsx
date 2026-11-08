import { render, screen, fireEvent, within } from '@testing-library/react'
import type { LegacyGoalLoop, StructuredMonitor } from '../../monitoring/automation'
import CrewOnItsOwn, { patrolLoopOf } from './CrewOnItsOwn'

/* The Schedules tab's "On its own" row: the patrol a crewmate runs over the
 * tasks it handed out, in the schedule row's grammar, with Pause / Resume as its
 * one control. What is pinned:
 *
 *   - only a `work-ledger` goal loop is a row here; a pull request watch, a
 *     plain timer or no loop at all is not;
 *   - the row says what the reader can act on (does it run, what wakes it, when
 *     it last and next checks, how many tasks are open) and none of the budget
 *     bookkeeping the Profile readout it replaces used to show;
 *   - a stop reads as one phrase; the four stop codes a person can act on are
 *     worded, every other code is the plain "Stopped" rather than a raw value.
 */

const NOW = 1_800_000_000

const LOOP: LegacyGoalLoop = {
  kind: 'legacy_goal_loop', id: 'ledger-1', slotKey: 'member-oncall', message: 'PROMPT-MARKER',
  createdAt: NOW - 358 * 3600, idleSecs: 900, maxCycles: 1000, cycleCount: 175,
  active: true, lastFireAt: NOW - 120, nextDueAt: NOW + 780,
  maxRuntimeSecs: 7 * 86400, stoppedReason: '', monitorOutcome: '', monitorKind: 'work-ledger',
  stopSentinelPath: '/tmp/SENTINEL-MARKER', judge: { wake_when: 'WAKE-MARKER' },
}

const PR_MONITOR: StructuredMonitor = {
  kind: 'structured_monitor', id: 'monitor-1', slotKey: 'member-oncall', active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', createdAt: NOW - 3600,
  cadenceSecs: 300, nextProbeAt: NOW + 300, wakeInstructions: '',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'pending', reasonCode: 'checks_pending', observedAt: NOW - 30, decision: 'no_change' },
  usage: { probes: 5, wakes: 1, agentTurns: 1, inputTokens: 120, outputTokens: 20, providerErrors: 0, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
}

// `timeAgo` reads the wall clock; pin it to the fixture's `nowTs`.
beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(NOW * 1000) })
afterEach(() => { vi.useRealTimers() })

function setup(overrides: Partial<Parameters<typeof CrewOnItsOwn>[0]> = {}) {
  const onPause = vi.fn()
  const onResume = vi.fn()
  render(
    <CrewOnItsOwn loop={LOOP} openTasks={3} nowTs={NOW} busy={false} error="" onPause={onPause} onResume={onResume} {...overrides} />,
  )
  return { onPause, onResume, row: screen.getByTestId('crew-patrol-row') }
}

describe('patrolLoopOf', () => {
  it('is the work-ledger loop, active or retained after a stop, and nothing else', () => {
    expect(patrolLoopOf(LOOP)).toBe(LOOP)
    const stopped = { ...LOOP, active: false, stoppedReason: 'runtime_budget' }
    expect(patrolLoopOf(stopped)).toBe(stopped)
    expect(patrolLoopOf({ ...LOOP, active: false, stoppedReason: '' })).toBeNull()
    // A FINISHED patrol (every item closed, or its stop file dropped) is no row:
    // the service refuses to revive it, so Resume would do nothing.
    expect(patrolLoopOf({ ...LOOP, active: false, stoppedReason: 'monitor_terminal' })).toBeNull()
    expect(patrolLoopOf({ ...LOOP, active: false, stoppedReason: 'stop_sentinel' })).toBeNull()
    expect(patrolLoopOf({ ...LOOP, monitorKind: 'gh-pr' })).toBeNull()
    expect(patrolLoopOf({ ...LOOP, monitorKind: '' })).toBeNull()
    expect(patrolLoopOf(PR_MONITOR)).toBeNull()
    expect(patrolLoopOf(null)).toBeNull()
  })
})

describe('CrewOnItsOwn', () => {
  it('a running patrol: name, Running, what wakes it, last and next check, open tasks, and Pause', () => {
    const { row, onPause, onResume } = setup()
    expect(row).toHaveAttribute('data-state', 'running')
    expect(within(row).getByTestId('crew-patrol-state')).toHaveTextContent('Running')
    expect(row).toHaveTextContent('Watching its tasks')
    expect(row).toHaveTextContent('Wakes when a task reports back, or every 15m')
    const facts = within(row).getByTestId('crew-patrol-facts')
    expect(facts).toHaveTextContent('Last check 2m ago · Next in 13m · 3 tasks open')
    const toggle = within(row).getByTestId('crew-patrol-toggle')
    expect(toggle).toHaveTextContent('Pause')
    fireEvent.click(toggle)
    expect(onPause).toHaveBeenCalledOnce()
    expect(onResume).not.toHaveBeenCalled()
  })

  it('shows none of the budget bookkeeping the Profile readout used to, and leaks no prompt or path', () => {
    const { row } = setup()
    const text = row.textContent ?? ''
    for (const gone of ['175', '1,000', '358', 'Runtime', 'Time used', 'Stopped because', 'Subject', 'Task list']) {
      expect(text).not.toContain(gone)
    }
    for (const marker of ['PROMPT-MARKER', 'SENTINEL-MARKER', 'WAKE-MARKER', 'member-oncall']) {
      expect(text).not.toContain(marker)
    }
  })

  it('a next check already in the past is not promised, and one task is singular', () => {
    const { row } = setup({ loop: { ...LOOP, nextDueAt: NOW - 13 * 86400 }, openTasks: 1 })
    expect(within(row).getByTestId('crew-patrol-facts')).toHaveTextContent(/^Last check 2m ago · 1 task open$/)
  })

  it('an unknown open count and a patrol that never checked read as such', () => {
    const { row } = setup({ loop: { ...LOOP, lastFireAt: 0 }, openTasks: null })
    expect(within(row).getByTestId('crew-patrol-facts')).toHaveTextContent(/^Has not checked yet · Next in 13m$/)
  })

  it.each([
    ['runtime_budget', 'Hit its 7d time limit'],
    ['cycle_cap', 'Hit its check limit'],
    ['manual', 'You paused it'],
    ['user_stop', 'You paused it'],
    ['approval_stalled', 'Stopped while waiting for your approval'],
    ['session_close', 'Its chat was closed'],
    ['future_reason', 'Stopped'],
  ])('a patrol stopped by %s reads Paused with one phrase and a Resume control', (stoppedReason, phrase) => {
    const { row, onPause, onResume } = setup({ loop: { ...LOOP, active: false, stoppedReason, nextDueAt: 0 } })
    expect(row).toHaveAttribute('data-state', 'paused')
    expect(within(row).getByTestId('crew-patrol-state')).toHaveTextContent('Paused')
    expect(within(row).getByTestId('crew-patrol-facts')).toHaveTextContent(`${phrase} · 3 tasks still open`)
    expect(row.textContent).not.toContain(stoppedReason)
    const toggle = within(row).getByTestId('crew-patrol-toggle')
    expect(toggle).toHaveTextContent('Resume')
    fireEvent.click(toggle)
    expect(onResume).toHaveBeenCalledOnce()
    expect(onPause).not.toHaveBeenCalled()
  })

  it.each([
    ['session_start_failures', 'It could not start a check. Resume to try again.'],
    ['session_unavailable', 'It could not start a check. Resume to try again.'],
    ['consecutive_failures', 'Its last check failed. Resume to try again.'],
    ['model_refused', 'Its last check failed. Resume to try again.'],
    ['sentinel_dropped', 'It could not keep watching the way it was set up. Resume to try again.'],
    ['verdict_stall', 'It could not keep watching the way it was set up. Resume to try again.'],
    ['invalid_monitor_record', 'It could not keep watching the way it was set up. Resume to try again.'],
  ])('a patrol that stopped because it FAILED (%s) says so as an error with its next step, not as "Stopped"', (stoppedReason, message) => {
    const { row } = setup({ loop: { ...LOOP, active: false, stoppedReason, nextDueAt: 0 } })
    expect(within(row).getByTestId('crew-patrol-state')).toHaveTextContent('Paused')
    expect(screen.getByTestId('crew-patrol-stop-error')).toHaveTextContent(message)
    expect(within(row).getByTestId('crew-patrol-facts')).toHaveTextContent(/^3 tasks still open$/)
    expect(row.textContent).not.toContain('Stopped')
    expect(within(row).getByTestId('crew-patrol-toggle')).toHaveTextContent('Resume')
  })

  it('a failed open-task count is said under the row, and the facts carry no count', () => {
    const { row } = setup({ openTasks: null, countFailed: true })
    expect(screen.getByTestId('crew-patrol-count-error')).toHaveTextContent('Could not count its open tasks.')
    expect(within(row).getByTestId('crew-patrol-facts')).toHaveTextContent(/^Last check 2m ago · Next in 13m$/)
  })

  it('a write in flight disables the control; a refused write shows as an error notice', () => {
    setup({ busy: true, error: 'Could not change it. Try again.' })
    expect(screen.getByTestId('crew-patrol-toggle')).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not change it. Try again.')
  })
})
