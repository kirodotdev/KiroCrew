/**
 * Unit tests for the Workflows "Runs" view-model helpers.
 *
 * These cover the pure functions that turn the gateway run registry
 * (`GET /api/workflows/runs`) into render-ready rows: status-badge mapping,
 * run-row summarization, and list ordering. Live fetch/polling and the run
 * detail view are out of scope here (covered by the live gates), matching the
 * deterministic-floor approach of WorkflowsPage.test.
 */
import { describe, it, expect } from 'vitest'
import {
  statusBadge,
  isCancellable,
  summarizeRuns,
  orderRuns,
  type RunStatus,
  type RunSummary,
} from '../apps/workflows/WorkflowsRuns'

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: 'wf_1',
    name: 'demo',
    status: 'finished' as RunStatus,
    result: null,
    error: null,
    author: null,
    session_key: null,
    event_count: 0,
    ...over,
  }
}

describe('statusBadge', () => {
  it('maps each known status to a variant + label + active flag', () => {
    expect(statusBadge('running')).toEqual({ variant: 'aim', label: 'Running', active: true })
    expect(statusBadge('finished')).toEqual({ variant: 'ok', label: 'Finished', active: false })
    expect(statusBadge('failed')).toEqual({ variant: 'err', label: 'Failed', active: false })
    expect(statusBadge('cancelled')).toEqual({ variant: 'warn', label: 'Cancelled', active: false })
  })

  it('only running is active (cancellable)', () => {
    expect(isCancellable('running')).toBe(true)
    expect(isCancellable('finished')).toBe(false)
    expect(isCancellable('failed')).toBe(false)
    expect(isCancellable('cancelled')).toBe(false)
  })

  it('falls back to a neutral warn badge for unknown/missing status', () => {
    expect(statusBadge(undefined)).toEqual({ variant: 'warn', label: 'Unknown', active: false })
    expect(statusBadge(null)).toEqual({ variant: 'warn', label: 'Unknown', active: false })
    expect(statusBadge('weird')).toEqual({ variant: 'warn', label: 'weird', active: false })
  })
})

describe('summarizeRuns', () => {
  it('builds a row per run with derived badge + cancellable flag', () => {
    const rows = summarizeRuns([
      run({ run_id: 'wf_a', name: 'alpha', status: 'running', event_count: 5, author: 'sam' }),
      run({ run_id: 'wf_b', name: 'beta', status: 'failed', error: 'boom', event_count: 2 }),
    ])
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      run_id: 'wf_a',
      name: 'alpha',
      status: 'running',
      author: 'sam',
      eventCount: 5,
      cancellable: true,
    })
    expect(rows[0].badge.variant).toBe('aim')
    expect(rows[1]).toMatchObject({ run_id: 'wf_b', status: 'failed', cancellable: false })
    expect(rows[1].badge.variant).toBe('err')
  })

  it('falls back to run_id when the name is empty or whitespace', () => {
    expect(summarizeRuns([run({ run_id: 'wf_x', name: '' })])[0].name).toBe('wf_x')
    expect(summarizeRuns([run({ run_id: 'wf_y', name: '   ' })])[0].name).toBe('wf_y')
  })

  it('reads an optional agent_count when the gateway attaches one', () => {
    const withAgents = { ...run({ run_id: 'wf_z' }), agent_count: 3 } as RunSummary
    expect(summarizeRuns([withAgents])[0].agentCount).toBe(3)
    // absent → defaults to 0, never NaN/undefined
    expect(summarizeRuns([run()])[0].agentCount).toBe(0)
  })

  it('handles null/undefined input gracefully', () => {
    expect(summarizeRuns(null)).toEqual([])
    expect(summarizeRuns(undefined)).toEqual([])
    expect(summarizeRuns([])).toEqual([])
  })

  it('coerces a non-numeric event_count to 0', () => {
    const bad = { ...run({ run_id: 'wf_e' }), event_count: undefined } as unknown as RunSummary
    expect(summarizeRuns([bad])[0].eventCount).toBe(0)
  })
})

describe('orderRuns', () => {
  it('pins running runs to the top while preserving server order otherwise', () => {
    const rows = summarizeRuns([
      run({ run_id: 'wf_1', status: 'finished' }),
      run({ run_id: 'wf_2', status: 'running' }),
      run({ run_id: 'wf_3', status: 'failed' }),
      run({ run_id: 'wf_4', status: 'running' }),
    ])
    const ordered = orderRuns(rows)
    expect(ordered.map(r => r.run_id)).toEqual(['wf_2', 'wf_4', 'wf_1', 'wf_3'])
  })

  it('is a no-op (order preserved) when nothing is running', () => {
    const rows = summarizeRuns([
      run({ run_id: 'wf_1', status: 'finished' }),
      run({ run_id: 'wf_2', status: 'failed' }),
      run({ run_id: 'wf_3', status: 'cancelled' }),
    ])
    expect(orderRuns(rows).map(r => r.run_id)).toEqual(['wf_1', 'wf_2', 'wf_3'])
  })

  it('does not mutate its input', () => {
    const rows = summarizeRuns([
      run({ run_id: 'wf_1', status: 'finished' }),
      run({ run_id: 'wf_2', status: 'running' }),
    ])
    const snapshot = rows.map(r => r.run_id)
    orderRuns(rows)
    expect(rows.map(r => r.run_id)).toEqual(snapshot)
  })

  it('handles an empty list', () => {
    expect(orderRuns([])).toEqual([])
  })
})
