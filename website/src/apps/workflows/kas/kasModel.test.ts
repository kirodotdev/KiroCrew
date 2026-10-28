import { describe, expect, it } from 'vitest'

import { kasControls, kasLoopRow, kasRow } from '../../../test/kasFixtures'
import {
  canDelete,
  canStop,
  defaultStep,
  displayBadge,
  dockSummary,
  findNode,
  findNodeByPath,
  humanizeInputName,
  isRejectOption,
  leafSteps,
  liveStatusLine,
  liveStepProgress,
  needsAttention,
  planStepLines,
  planSummary,
  primaryControl,
  runElapsedMs,
  showReconnect,
  stepAwaitsReply,
  stepInstances,
  stepProgress,
  stepRetryable,
  windowRuns,
} from './kasModel'
import type { KasRunCapability } from '../../../api/client'
import { getKasSelection, resetKasSelections, setKasSelection } from './kasSelection'

describe('kasModel — controls follow row.controls', () => {
  it('drops a primary or Stop/Delete that run.actions does not list', () => {
    // An exhausted loop is paused but KAS takes no plain Resume there.
    const exhausted = kasRow({ status: 'paused', controls: kasControls({ display: 'paused', primary: 'resume' }), actions: ['cancel'] })
    expect(primaryControl(exhausted)).toBeNull()
    expect(canStop(exhausted)).toBe(true)
    expect(primaryControl(kasRow({ controls: kasControls({ primary: 'pause' }), actions: ['cancel'] }))).toBeNull()
    expect(canStop(kasRow({ controls: kasControls({ primary: 'pause' }), actions: ['pause'] }))).toBe(false)
    const finished = kasRow({ status: 'completed', controls: kasControls({ display: 'completed', primary: null, canStop: false, canDelete: true }) })
    expect(canDelete({ ...finished, actions: ['restart'] })).toBe(false)
    expect(canDelete({ ...finished, actions: ['restart', 'delete'] })).toBe(true)
    // A row without the list (an older gateway) keeps what `controls` says.
    expect(primaryControl(kasRow({ status: 'paused', controls: kasControls({ display: 'paused', primary: 'resume' }), actions: undefined }))).toEqual({ kind: 'resume' })
  })

  it('offers a lone-step Retry only outside loop iterations, for failed or stopped steps', () => {
    const row = { actions: ['retry', 'restart', 'delete'] as KasRunCapability[] }
    expect(stepRetryable(row, { status: 'failed', path: 'review' })).toBe(true)
    expect(stepRetryable(row, { status: 'aborted', path: 'inc/inc/test' })).toBe(true)
    expect(stepRetryable(row, { status: 'failed', path: 'loop/iter-2/code' })).toBe(false)
    expect(stepRetryable(row, { status: 'completed', path: 'review' })).toBe(false)
    expect(stepRetryable({ actions: ['restart', 'delete'] }, { status: 'failed', path: 'review' })).toBe(false)
  })
  it('maps each primary to exactly one button kind', () => {
    expect(primaryControl(kasRow({ controls: kasControls({ primary: 'pause' }) }))).toEqual({ kind: 'pause' })
    expect(primaryControl(kasRow({ status: 'paused', controls: kasControls({ display: 'paused', primary: 'resume' }) }))).toEqual({ kind: 'resume' })
    expect(primaryControl(kasRow({ controls: kasControls({ primary: 'open', needsAttention: true }) }))).toEqual({ kind: 'open' })
  })

  it('shows a disabled Pausing placeholder only when the server offers no primary while pausing', () => {
    const pausing = kasRow({ pausePending: true, controls: kasControls({ display: 'pausing', primary: null }) })
    expect(primaryControl(pausing)).toEqual({ kind: 'pausing' })
    expect(primaryControl(kasRow({ controls: kasControls({ display: 'running', primary: null }) }))).toBeNull()
  })

  it('offers nothing but Delete on a terminal run, whatever controls claims', () => {
    for (const status of ['completed', 'failed', 'aborted'] as const) {
      const row = kasRow({ status, controls: kasControls({ display: 'completed', primary: 'pause', canStop: true, canDelete: true }) })
      expect(primaryControl(row)).toBeNull()
      expect(canStop(row)).toBe(false)
      expect(canDelete(row)).toBe(true)
    }
  })

  it('never offers Delete on a live run', () => {
    expect(canDelete(kasRow({ controls: kasControls({ canDelete: true }) }))).toBe(false)
  })

  it('hides Stop when the server says it cannot stop', () => {
    expect(canStop(kasRow({ controls: kasControls({ canStop: false }) }))).toBe(false)
    expect(canStop(kasRow())).toBe(true)
  })
})

describe('kasModel — reconnect visibility', () => {
  it('shows Reconnect only for an undriven, unfinished run that has a chat', () => {
    expect(showReconnect(kasRow({ driven: false }))).toBe(true)
    expect(showReconnect(kasRow({ driven: true }))).toBe(false)
    // `driven` absent (an older payload) is not evidence the run is orphaned.
    expect(showReconnect(kasRow({ driven: undefined }))).toBe(false)
    expect(showReconnect(kasRow({ driven: false, slotKey: null }))).toBe(false)
    expect(showReconnect(kasRow({ driven: false, status: 'completed' }))).toBe(false)
  })

  it('offers no Pause, Resume or Stop on an undriven run: the server would refuse them', () => {
    const orphan = kasRow({ driven: false, controls: kasControls({ primary: 'pause', canStop: true }) })
    expect(primaryControl(orphan)).toBeNull()
    expect(canStop(orphan)).toBe(false)
    expect(primaryControl(kasRow({ driven: undefined, controls: kasControls({ primary: 'pause' }) }))).toEqual({
      kind: 'pause',
    })
  })
})

describe('kasModel — badges and progress', () => {
  it('draws stopped grey and failed red', () => {
    expect(displayBadge('stopped').variant).toBe('muted')
    expect(displayBadge('failed').variant).toBe('err')
    expect(displayBadge('completed').variant).toBe('ok')
    expect(displayBadge('pausing').variant).toBe('warn')
  })

  it('reports Step n/m only when both counts are known', () => {
    expect(stepProgress(kasRow({ completedSteps: 2, totalSteps: 5 }))).toEqual({ done: 2, total: 5 })
    expect(stepProgress(kasRow({ completedSteps: null, totalSteps: null }))).toBeNull()
    expect(stepProgress(kasRow({ completedSteps: 0, totalSteps: 0 }))).toBeNull()
  })
})

describe('kasModel — approvals', () => {
  it('treats both reject kinds as destructive', () => {
    expect(isRejectOption({ kind: 'reject_once' })).toBe(true)
    expect(isRejectOption({ kind: 'reject_always' })).toBe(true)
    expect(isRejectOption({ kind: 'allow_once' })).toBe(false)
  })

  it('flattens nested plan steps across steps/branches with depth', () => {
    const lines = planStepLines({
      kind: 'plan',
      steps: [
        { id: 'a', label: 'Plan' },
        { id: 'p', type: 'parallel', branches: [{ id: 'b1', agent: 'reviewer' }, { id: 'b2', prompt: 'Lint it' }] },
      ],
    })
    expect(lines.map(l => [l.label, l.depth])).toEqual([
      ['Plan', 0],
      ['p', 0],
      ['reviewer', 1],
      ['Lint it', 1],
    ])
  })

  it('returns no lines for a missing or malformed plan', () => {
    expect(planStepLines(null)).toEqual([])
    expect(planStepLines({ steps: 'nope' })).toEqual([])
  })
})

describe('kasModel — IDE run card / rail helpers', () => {
  it('planSummary names at most four top-level labels', () => {
    expect(planSummary(kasRow())).toEqual({ count: 2, labels: ['Plan', 'Fix loop'], more: false })
    expect(planSummary(kasRow({ nodes: [] }))).toBeNull()
  })

  it('findNode and leafSteps walk the nested tree', () => {
    expect(findNode(kasRow().nodes, 'fix')?.agent).toBe('fixer')
    expect(findNode(kasRow().nodes, 'nope')).toBeNull()
    expect(leafSteps(kasRow().nodes).map(n => n.id)).toEqual(['plan', 'review', 'fix'])
  })

  it('liveStatusLine: blocking, else the active step with its agent, else the pause reason', () => {
    expect(liveStatusLine(kasRow({ blocking: { nodeId: 'review', label: 'Review', kind: 'permission', sessionId: null, toolCallId: null } }))).toEqual({
      kind: 'blocking',
      text: 'Review — Waiting for approval',
    })
    expect(liveStatusLine(kasRow())).toEqual({ kind: 'active', text: 'Review', agent: 'reviewer' })
    expect(liveStatusLine(kasRow({ activeSteps: [], pauseReason: 'Paused by you', controls: kasControls({ display: 'paused' }) }))).toEqual({
      kind: 'reason',
      text: 'Paused by you',
    })
    expect(liveStatusLine(kasRow({ activeSteps: [] }))).toBeNull()
  })

  it('liveStepProgress hides on a finished run', () => {
    expect(liveStepProgress(kasRow())).toEqual({ done: 1, total: 3 })
    expect(liveStepProgress(kasRow({ status: 'completed' }))).toBeNull()
  })

  it('runElapsedMs runs from start (or creation) to end (or now)', () => {
    const now = Date.parse('2026-10-16T10:00:11Z')
    expect(runElapsedMs(kasRow(), now)).toBe(10_000)
    expect(runElapsedMs(kasRow({ startedAt: null }), now)).toBe(11_000)
    expect(runElapsedMs(kasRow({ endedAt: '2026-10-16T10:00:03Z' }), now)).toBe(2_000)
    expect(runElapsedMs(kasRow({ startedAt: null, createdAt: null }), now)).toBeNull()
  })

  it('needsAttention reads controls or blocking', () => {
    expect(needsAttention(kasRow())).toBe(false)
    expect(needsAttention(kasRow({ controls: kasControls({ needsAttention: true }) }))).toBe(true)
  })

  it('windowRuns keeps every live run, then the newest finished ones up to the limit', () => {
    const done = (i: number) => kasRow({ workflowId: `d${i}`, status: 'completed', updatedAt: `2026-10-15T00:0${i}:00Z` })
    const live = kasRow({ workflowId: 'live', updatedAt: '2026-10-01T00:00:00Z' })
    expect(windowRuns([done(1), live, done(3), done(2)], 2).map(r => r.workflowId)).toEqual(['live', 'd3', 'd2'])
  })

  it('defaultStep: blocking, else active, else the last finished step with a session', () => {
    expect(defaultStep(kasRow())).toEqual({ nodeId: 'review', path: 'loop/review' })
    expect(defaultStep(kasRow({ blocking: { nodeId: 'fix', label: 'Fix', kind: 'question', sessionId: null, toolCallId: null } }))).toEqual({
      nodeId: 'fix',
      path: 'loop/fix',
    })
    expect(defaultStep(kasRow({ activeSteps: [] }))).toEqual({ nodeId: 'plan', path: 'plan' })
    expect(defaultStep(kasRow({ activeSteps: [], nodes: [] }))).toBeNull()
  })

  it('defaultStep resolves a loop step to its LATEST iteration instance by session', () => {
    // The active Review runs in iteration 2 (session s-review-1).
    expect(defaultStep(kasLoopRow())).toEqual({ nodeId: 'review', path: 'loop/iter-1/review' })
  })

  it('findNodeByPath reaches a loop-body instance by its iteration path', () => {
    const row = kasLoopRow()
    expect(findNodeByPath(row.nodes, 'loop/iter-0/fix')?.status).toBe('completed')
    expect(findNodeByPath(row.nodes, 'loop/iter-1/review')?.status).toBe('running')
    expect(findNodeByPath(row.nodes, 'nope')).toBeNull()
  })

  it('stepInstances lists each iteration body once, latest last', () => {
    const paths = stepInstances(kasLoopRow().nodes).map(i => i.path)
    expect(paths).toEqual([
      'plan',
      'loop/iter-0/review',
      'loop/iter-0/fix',
      'loop/iter-1/review',
      'loop/iter-1/fix',
    ])
  })

  it('stepAwaitsReply: paused instance or a toolCall-less question on its session, never running/terminal', () => {
    const paused = { status: 'paused', sessionId: 's-x' }
    const running = { status: 'running', sessionId: 's-x' }
    const idle = { status: 'pending', sessionId: 's-x' }
    expect(stepAwaitsReply(kasRow(), paused)).toBe(true)
    expect(stepAwaitsReply(kasRow(), running)).toBe(false)
    expect(stepAwaitsReply(kasRow(), idle)).toBe(false)
    expect(
      stepAwaitsReply(
        kasRow({ attention: [{ kind: 'question', nodeId: 'n', sessionId: 's-x', nodePath: [], since: null, toolCallId: null }] }),
        idle,
      ),
    ).toBe(true)
    // A question WITH a tool call id is answered on the request, not by a message.
    expect(
      stepAwaitsReply(
        kasRow({ attention: [{ kind: 'question', nodeId: 'n', sessionId: 's-x', nodePath: [], since: null, toolCallId: 'tc' }] }),
        idle,
      ),
    ).toBe(false)
    expect(stepAwaitsReply(kasRow({ status: 'completed' }), paused)).toBe(false)
    expect(stepAwaitsReply(kasRow(), null)).toBe(false)
  })
})

describe('kasModel — G1 Kiro Agent step counts', () => {
  it('stepProgress uses Kiro Agent own counts when known (stepsTotal !== null), ours when absent', () => {
    // Known: use stepsCompleted/stepsTotal, not our loop-inflated count.
    expect(stepProgress(kasRow({ stepsCompleted: 1, stepsTotal: 2, completedSteps: 3, totalSteps: 5 }))).toEqual({ done: 1, total: 2 })
    // Absent (all four null): fall back to our own completed/total.
    expect(stepProgress(kasRow({ stepsCompleted: null, stepsTotal: null, completedSteps: 3, totalSteps: 5 }))).toEqual({ done: 3, total: 5 })
    // Known with a zero total still reads as no progress.
    expect(stepProgress(kasRow({ stepsCompleted: 0, stepsTotal: 0, completedSteps: 3, totalSteps: 5 }))).toBeNull()
  })

  it('liveStepProgress clears on a terminal run even when Kiro Agent sent counts', () => {
    expect(liveStepProgress(kasRow({ stepsCompleted: 1, stepsTotal: 2 }))).toEqual({ done: 1, total: 2 })
    expect(liveStepProgress(kasRow({ status: 'completed', stepsCompleted: 1, stepsTotal: 2 }))).toBeNull()
  })

  it('liveStatusLine prefers Kiro Agent own current node, and shows Waiting on <watch>', () => {
    // currentNodeId names the step over activeSteps[0] when known.
    expect(liveStatusLine(kasRow({ stepsTotal: 3, currentNodeId: 'fix', waitingOnWatch: false }))).toEqual({ kind: 'active', text: 'Fix', agent: 'fixer' })
    // waitingOnWatch: a watch runs with no step — the "Waiting on <watch>" line, no agent.
    expect(liveStatusLine(kasRow({ stepsTotal: 3, currentNodeId: 'review', waitingOnWatch: true }))).toEqual({ kind: 'active', text: 'Waiting on Review', agent: null })
    // Absent fields (older kiro-cli): unchanged — the active step from activeSteps[0].
    expect(liveStatusLine(kasRow({ stepsTotal: null, currentNodeId: null, waitingOnWatch: null }))).toEqual({ kind: 'active', text: 'Review', agent: 'reviewer' })
  })

  it('dockSummary aggregates Kiro Agent own counts when known, ours when absent', () => {
    const a = kasRow({ workflowId: 'a', stepsCompleted: 1, stepsTotal: 2, completedSteps: 9, totalSteps: 9 })
    const b = kasRow({ workflowId: 'b', stepsCompleted: null, stepsTotal: null, completedSteps: 3, totalSteps: 4 })
    const sum = dockSummary([a, b])
    expect(sum.stepsDone).toBe(4)
    expect(sum.stepsTotal).toBe(6)
  })
})

describe('humanizeInputName (CX r8)', () => {
  it('splits, sentence-cases, and keeps known acronyms upper', () => {
    expect(humanizeInputName('pr_url')).toBe('PR URL')
    expect(humanizeInputName('task')).toBe('Task')
    expect(humanizeInputName('workItemId')).toBe('Work item ID')
    expect(humanizeInputName('api_key')).toBe('API key')
    expect(humanizeInputName('ci')).toBe('CI')
    expect(humanizeInputName('user-name')).toBe('User name')
    expect(humanizeInputName('base_branch_name')).toBe('Base branch name')
  })

  it('returns the raw name when there is nothing to split', () => {
    expect(humanizeInputName('')).toBe('')
    expect(humanizeInputName('__')).toBe('__')
  })
})

describe('kasSelection', () => {
  it('is per chat, and null returns to the list', () => {
    resetKasSelections()
    setKasSelection('chat-1', { workflowId: 'wf-1' })
    setKasSelection('chat-2', { workflowId: 'wf-2', nodeId: 'n' })
    expect(getKasSelection('chat-1')).toEqual({ workflowId: 'wf-1' })
    expect(getKasSelection('chat-2')).toEqual({ workflowId: 'wf-2', nodeId: 'n' })
    setKasSelection('chat-1', null)
    expect(getKasSelection('chat-1')).toBeNull()
    expect(getKasSelection(null)).toBeNull()
    resetKasSelections()
  })
})
