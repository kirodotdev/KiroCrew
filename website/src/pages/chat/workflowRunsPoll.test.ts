import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  WORKFLOW_RUNS_FAST_POLL_MS,
  WORKFLOW_RUNS_HEAL_POLL_MS,
  workflowRunsPollInterval,
} from './workflowRunsPoll'

/* ── The side panel's workflow-runs list poll runs fast only on the Workflows
 *    view or while a run of this chat is running, and otherwise ticks at the
 *    socket healer's slow rate. ── */

const SLOT = 'chat-abc'

describe('workflowRunsPollInterval', () => {
  it('polls fast on the Workflows view even with no runs', () => {
    expect(workflowRunsPollInterval('workflows', [], SLOT)).toBe(WORKFLOW_RUNS_FAST_POLL_MS)
    expect(workflowRunsPollInterval('workflows', undefined, SLOT)).toBe(WORKFLOW_RUNS_FAST_POLL_MS)
  })

  it('ticks at the heal rate on another view with no running run', () => {
    for (const view of ['changes', 'subagents', 'logs', 'links', undefined]) {
      expect(workflowRunsPollInterval(view, [], SLOT)).toBe(WORKFLOW_RUNS_HEAL_POLL_MS)
    }
    expect(workflowRunsPollInterval('changes', undefined, SLOT)).toBe(WORKFLOW_RUNS_HEAL_POLL_MS)
    expect(workflowRunsPollInterval('changes', [{ status: 'finished', session_key: SLOT }], SLOT))
      .toBe(WORKFLOW_RUNS_HEAL_POLL_MS)
  })

  it('polls fast on another view while a run of this slot is running', () => {
    expect(workflowRunsPollInterval('changes', [{ status: 'running', session_key: SLOT }], SLOT))
      .toBe(WORKFLOW_RUNS_FAST_POLL_MS)
    expect(workflowRunsPollInterval('changes', [{ status: 'running', session_key: `dashboard:${SLOT}` }], SLOT))
      .toBe(WORKFLOW_RUNS_FAST_POLL_MS)
  })

  it('ignores a running run that belongs to another slot', () => {
    expect(workflowRunsPollInterval('changes', [{ status: 'running', session_key: 'chat-other' }], SLOT))
      .toBe(WORKFLOW_RUNS_HEAL_POLL_MS)
    expect(workflowRunsPollInterval('changes', [{ status: 'running' }], SLOT))
      .toBe(WORKFLOW_RUNS_HEAL_POLL_MS)
  })

  it('heals at the socket healer cadence', () => {
    const healer = readFileSync(join(__dirname, '..', '..', 'hooks', 'websocket', 'workflowRuns.ts'), 'utf8')
    expect(healer).toMatch(new RegExp(`WORKFLOW_HEAL_MS = ${WORKFLOW_RUNS_HEAL_POLL_MS}\\b`))
  })

  it('is the interval ActivityViewer polls with, and a hidden tab skips the tick', () => {
    const src = readFileSync(join(__dirname, 'ActivityViewer.tsx'), 'utf8')
    const start = src.indexOf("queryKey: ['workflow-runs']")
    expect(start).toBeGreaterThan(-1)
    const block = src.slice(start, src.indexOf('})', start))
    expect(block).toMatch(/refetchInterval: query => workflowRunsPollInterval\(view \?\? tab, query\.state\.data, slot\)/)
    expect(block).toMatch(/refetchIntervalInBackground: false/)
  })
})
