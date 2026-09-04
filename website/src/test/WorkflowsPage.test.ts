/**
 * Unit tests for the Workflows-tab event-stream view-model helpers.
 *
 * These cover the pure folding logic that turns the run event stream into the
 * live phase tree + budget gauge the Workflows page renders. The full
 * tab/run-view/WS behavior is covered by the E1–E4 Playwright gates against the
 * dev instance; these are the deterministic floor under them.
 */
import { describe, it, expect } from 'vitest'
import { groupByPhase, latestBudget } from '../apps/workflows/WorkflowsPage'

function ev(type: string, data: Record<string, unknown> = {}, seq = 0) {
  return { run_id: 'wf_t', seq, ts: 't', type, data }
}

describe('groupByPhase', () => {
  it('groups agents under their phase in order', () => {
    const events = [
      ev('run_started', { name: 'x', budget_total: null }),
      ev('phase_started', { title: 'Review' }),
      ev('agent_started', { agent_id: 'a0', label: 'review:bugs', phase: 'Review' }),
      ev('agent_finished', { agent_id: 'a0', ok: true }),
      ev('phase_started', { title: 'Verify' }),
      ev('agent_started', { agent_id: 'a1', label: 'verify:x', phase: 'Verify' }),
    ]
    const phases = groupByPhase(events)
    expect(phases.map(p => p.title)).toEqual(['Review', 'Verify'])
    expect(phases[0].agents[0]).toMatchObject({ agent_id: 'a0', label: 'review:bugs', ok: true })
    // a1 has no agent_finished yet → ok is undefined (renders as "running")
    expect(phases[1].agents[0].ok).toBeUndefined()
  })

  it('tracks last_tool from agent_progress', () => {
    const phases = groupByPhase([
      ev('agent_started', { agent_id: 'a0', label: 'go', phase: '' }),
      ev('agent_progress', { agent_id: 'a0', last_tool: 'grep' }),
    ])
    expect(phases[0].agents[0].last_tool).toBe('grep')
  })

  it('handles an empty stream', () => {
    expect(groupByPhase([])).toEqual([])
  })

  it('places agents with no phase under the empty-title group', () => {
    const phases = groupByPhase([
      ev('agent_started', { agent_id: 'a0', label: 'x', phase: '' }),
    ])
    expect(phases[0].title).toBe('')
    expect(phases[0].agents).toHaveLength(1)
  })
})

describe('latestBudget', () => {
  it('seeds from run_started and updates on budget_update', () => {
    const b = latestBudget([
      ev('run_started', { budget_total: 5000 }),
      ev('budget_update', { spent: 1200, remaining: 3800 }),
      ev('budget_update', { spent: 2500, remaining: 2500 }),
    ])
    expect(b).toEqual({ spent: 2500, total: 5000 })
  })

  it('is null when no budget events present', () => {
    expect(latestBudget([ev('log', { message: 'hi' })])).toBeNull()
  })

  it('handles an unbounded budget (total null)', () => {
    const b = latestBudget([ev('run_started', { budget_total: null })])
    expect(b).toEqual({ spent: 0, total: null })
  })
})
