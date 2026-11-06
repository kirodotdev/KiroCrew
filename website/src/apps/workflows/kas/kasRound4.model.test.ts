import { describe, expect, it } from 'vitest'

import { kasControls, kasLoopExhaustedRow, kasRow } from '../../../test/kasFixtures'
import type { KasRunNode } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import {
  dockSummary,
  groupSummary,
  needsAttention,
  nodeWaitLabel,
  orderRuns,
  planUpdateIsWarning,
  planUpdateLine,
  retryWaitLine,
  runBadge,
  showNeedsYouPill,
  splitRuns,
  stepDurationMs,
  watchOutcomeLabel,
  windowRuns,
} from './kasModel'

const leaf = (over: Partial<KasRunNode> & Pick<KasRunNode, 'id'>): KasRunNode => ({
  path: over.id,
  type: 'step',
  agent: null,
  label: over.id,
  maxIterations: null,
  status: 'pending',
  sessionId: null,
  iteration: null,
  reason: null,
  children: [],
  ...over,
})

describe('gap 7 — needs-you badge and pill', () => {
  it('a running run that needs a person reads "Needs you", not "Paused"', () => {
    const blocked = kasRow({ controls: kasControls({ display: 'running', needsAttention: true }) })
    expect(runBadge(blocked).label).toBe(i18nT('apps.workflows.kas.state_needs_you'))
    expect(runBadge(blocked).variant).toBe('warn')
    // No separate pill when the badge already says it.
    expect(showNeedsYouPill(blocked)).toBe(false)
  })

  it('a non-user park keeps "Paused" and carries the separate needs-you pill', () => {
    const exhausted = kasLoopExhaustedRow() // display 'paused', needsAttention true
    expect(runBadge(exhausted).label).toBe(i18nT('apps.workflows.kas.state_paused'))
    expect(showNeedsYouPill(exhausted)).toBe(true)
  })

  it('a quiet run shows neither', () => {
    const quiet = kasRow()
    expect(runBadge(quiet).label).toBe(i18nT('apps.workflows.kas.state_running'))
    expect(showNeedsYouPill(quiet)).toBe(false)
  })

  it('dockSummary counts every needs-you run by the one unified rule', () => {
    const rows = [
      kasRow({ workflowId: 'a', controls: kasControls({ display: 'running', needsAttention: true }) }),
      kasLoopExhaustedRow({ workflowId: 'b' }),
      kasRow({ workflowId: 'c' }),
    ]
    expect(dockSummary(rows).needsYou).toBe(2)
    expect(rows.every(needsAttention)).toBe(false)
  })
})

describe('gap 22 — stable order by created/ended, not updated', () => {
  it('a step event (newer updatedAt) does not move a live run above an older-created one', () => {
    const older = kasRow({ workflowId: 'old', createdAt: '2026-10-16T10:00:00Z', updatedAt: '2026-10-16T10:09:00Z' })
    const newer = kasRow({ workflowId: 'new', createdAt: '2026-10-16T10:05:00Z', updatedAt: '2026-10-16T10:06:00Z' })
    expect(orderRuns([older, newer]).map(r => r.workflowId)).toEqual(['new', 'old'])
  })

  it('splits live before finished and windows the finished tail', () => {
    const live = kasRow({ workflowId: 'live', status: 'running' })
    const done = Array.from({ length: 12 }, (_, i) =>
      kasRow({ workflowId: `d${i}`, status: 'completed', endedAt: `2026-10-16T10:${String(10 + i).padStart(2, '0')}:00Z`, controls: kasControls({ display: 'completed', canStop: false }) }),
    )
    const { live: liveRows, done: doneRows } = splitRuns([...done, live])
    expect(liveRows.map(r => r.workflowId)).toEqual(['live'])
    expect(doneRows).toHaveLength(12)
    expect(windowRuns([...done, live], 10)).toHaveLength(11) // 1 live + 10 finished
  })
})

describe('gap 11 — plan-update line', () => {
  it('names applied / rejected / dropped and flags a rejection', () => {
    expect(planUpdateLine(kasRow({ lastPlanUpdate: { outcome: 'applied', reason: null, at: 'x' } }))).toBe(i18nT('apps.workflows.kas.plan_updated'))
    expect(planUpdateLine(kasRow({ lastPlanUpdate: { outcome: 'rejected', reason: 'too risky', at: 'x' } }))).toBe(
      i18nT('apps.workflows.kas.plan_update_rejected', { reason: 'too risky' }),
    )
    expect(planUpdateLine(kasRow({ lastPlanUpdate: { outcome: 'dropped', reason: null, at: 'x' } }))).toBe(i18nT('apps.workflows.kas.plan_update_dropped'))
    expect(planUpdateLine(kasRow())).toBeNull()
    expect(planUpdateIsWarning(kasRow({ lastPlanUpdate: { outcome: 'rejected', reason: null, at: 'x' } }))).toBe(true)
    expect(planUpdateIsWarning(kasRow({ lastPlanUpdate: { outcome: 'applied', reason: null, at: 'x' } }))).toBe(false)
  })
})

describe('gap 1 / 23 — step-row lines', () => {
  it('retryWaitLine shows KAS backoff only when the node is in retry-wait', () => {
    expect(retryWaitLine(leaf({ id: 's', retryWait: { reason: 'rate limited', since: 't' } }))).toBe(
      i18nT('apps.workflows.kas.retry_wait', { reason: 'rate limited' }),
    )
    expect(retryWaitLine(leaf({ id: 's' }))).toBeNull()
  })

  it('watchOutcomeLabel maps the last poll, and only for a watch node', () => {
    expect(watchOutcomeLabel({ type: 'watch', watchOutcome: { outcome: 'new-activity', at: null } })).toBe(i18nT('apps.workflows.kas.watch_new_activity'))
    expect(watchOutcomeLabel({ type: 'watch', watchOutcome: { outcome: 'idle', at: null } })).toBe(i18nT('apps.workflows.kas.watch_idle'))
    expect(watchOutcomeLabel({ type: 'step', watchOutcome: { outcome: 'idle', at: null } })).toBeNull()
  })

  it('nodeWaitLabel reads KAS\u2019s wait flag', () => {
    expect(nodeWaitLabel({ waitingFor: 'approval' })).toBe(i18nT('apps.workflows.kas.needs_approval'))
    expect(nodeWaitLabel({ waitingFor: 'input' })).toBe(i18nT('apps.workflows.kas.waiting_for_you'))
    expect(nodeWaitLabel({ waitingFor: null })).toBeNull()
  })

  it('stepDurationMs spans started->ended, else started->now', () => {
    const start = '2026-10-16T10:00:00Z'
    expect(stepDurationMs({ startedAt: start, endedAt: '2026-10-16T10:02:10Z', status: 'completed' }, 0)).toBe(130_000)
    expect(stepDurationMs({ startedAt: null, endedAt: null, status: 'running' }, 0)).toBeNull()
  })

  it('groupSummary flags input first, then failures, then a count', () => {
    const grp = (kids: KasRunNode[]): Pick<KasRunNode, 'children' | 'iterations' | 'type'> => ({ type: 'sequence', children: kids, iterations: undefined })
    expect(groupSummary(grp([leaf({ id: 'a', waitingFor: 'input' }), leaf({ id: 'b' })]))).toBe(i18nT('apps.workflows.kas.group_summary_input', { count: 1 }))
    expect(groupSummary(grp([leaf({ id: 'a', status: 'failed' }), leaf({ id: 'b', status: 'completed' })]))).toBe(i18nT('apps.workflows.kas.group_summary_failed', { count: 1 }))
    expect(groupSummary(grp([leaf({ id: 'a', status: 'completed' }), leaf({ id: 'b', status: 'completed' })]))).toBe(i18nT('apps.workflows.kas.group_summary_completed'))
    expect(groupSummary(grp([leaf({ id: 'a' }), leaf({ id: 'b' })]))).toBe(i18nT('apps.workflows.kas.group_summary_steps', { count: 2 }))
  })
})
