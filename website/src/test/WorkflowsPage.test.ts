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

describe('groupByPhase per-agent timing', () => {
  /** Same shape as `ev`, but with a real `ts` so a span can be measured. */
  function at(type: string, ts: string, data: Record<string, unknown> = {}, seq = 0) {
    return { run_id: 'wf_t', seq, ts, type, data }
  }

  it('derives the span from the ts the two events already carry', () => {
    const phases = groupByPhase([
      at('agent_started', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', phase: '' }),
      at('agent_finished', '2026-09-18T10:00:04.250Z', { agent_id: 'a0', ok: true }),
    ])
    expect(phases[0].agents[0].elapsed_ms).toBe(4250)
  })

  it('leaves a still-running agent with no span', () => {
    const phases = groupByPhase([
      at('agent_started', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', phase: '' }),
    ])
    expect(phases[0].agents[0].elapsed_ms).toBeUndefined()
  })

  it('drops a backwards span rather than reporting a negative time', () => {
    // The wire carries whatever clock the producer had, so finish-before-start is
    // reachable. A backwards duration is worse than none.
    const phases = groupByPhase([
      at('agent_started', '2026-09-18T10:00:05.000Z', { agent_id: 'a0', phase: '' }),
      at('agent_finished', '2026-09-18T10:00:01.000Z', { agent_id: 'a0', ok: true }),
    ])
    expect(phases[0].agents[0].elapsed_ms).toBeUndefined()
  })

  it('reports a zero span rather than dropping it', () => {
    // Distinct from the unmeasurable cases: both events landed in the same
    // millisecond, which is a real measurement of a very fast agent.
    const phases = groupByPhase([
      at('agent_started', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', phase: '' }),
      at('agent_finished', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', ok: true }),
    ])
    expect(phases[0].agents[0].elapsed_ms).toBe(0)
  })

  it('leaves no span when a ts does not parse', () => {
    // Every other test in this file uses ts: 't', so this is the case the
    // pre-existing suite exercises throughout.
    const phases = groupByPhase([
      at('agent_started', 't', { agent_id: 'a0', phase: '' }),
      at('agent_finished', 't', { agent_id: 'a0', ok: true }),
    ])
    expect(phases[0].agents[0].elapsed_ms).toBeUndefined()
  })

  it('leaves no span when the stream is truncated before the start', () => {
    const phases = groupByPhase([
      at('phase_started', '2026-09-18T10:00:00.000Z', { title: 'P' }),
      at('agent_finished', '2026-09-18T10:00:09.000Z', { agent_id: 'gone', ok: true }),
    ])
    expect(phases[0].agents).toHaveLength(0)
  })

  it('measures each agent independently when several overlap', () => {
    const phases = groupByPhase([
      at('agent_started', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', phase: '' }),
      at('agent_started', '2026-09-18T10:00:01.000Z', { agent_id: 'a1', phase: '' }),
      at('agent_finished', '2026-09-18T10:00:09.000Z', { agent_id: 'a1', ok: true }),
      at('agent_finished', '2026-09-18T10:00:12.000Z', { agent_id: 'a0', ok: true }),
    ])
    const byId = new Map(phases[0].agents.map(a => [a.agent_id, a.elapsed_ms]))
    expect(byId.get('a0')).toBe(12_000)
    expect(byId.get('a1')).toBe(8_000)
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
