import { describe, it, expect, vi } from 'vitest'

import type { KasRecipe, KasRunRow } from '../api/client'
import {
  isKasWorkflowCommand,
  parseKasWorkflowCommand,
  resolveRunTarget,
  runKasWorkflowCommand,
} from '../components/chat-input/kasSlashCommands'

function run(id: string, status: KasRunRow['status']): KasRunRow {
  return { workflowId: id, status, slotKey: 's1' } as unknown as KasRunRow
}

const reasons = { noSlot: 'NO_SLOT', noRun: 'NO_RUN', ambiguous: 'AMBIGUOUS' }

describe('parseKasWorkflowCommand (gap 15)', () => {
  it('parses each verb and ignores Crew /workflow and /workflows', () => {
    expect(parseKasWorkflowCommand('/workflow-status')).toEqual({ verb: 'status' })
    expect(parseKasWorkflowCommand('/workflow-resume')).toEqual({ verb: 'resume', runId: null })
    expect(parseKasWorkflowCommand('/workflow-resume wf_42')).toEqual({ verb: 'resume', runId: 'wf_42' })
    expect(parseKasWorkflowCommand('/workflow-cancel wf_9')).toEqual({ verb: 'cancel', runId: 'wf_9' })
    // Not KAS commands — must fall through so Crew's own runner still gets them.
    expect(parseKasWorkflowCommand('/workflow')).toBeNull()
    expect(parseKasWorkflowCommand('/workflows')).toBeNull()
    expect(parseKasWorkflowCommand('/workflow my-saved')).toBeNull()
    expect(parseKasWorkflowCommand('/workflow-foo')).toBeNull()
    expect(isKasWorkflowCommand('/workflow-run code-review')).toBe(true)
    expect(isKasWorkflowCommand('/workflow')).toBe(false)
  })

  it('parses a run recipe with key=value inputs (quoted and bare)', () => {
    expect(parseKasWorkflowCommand('/workflow-run code-review pr="123 abc" strict=yes bare')).toEqual({
      verb: 'run',
      recipe: 'code-review',
      inputs: { pr: '123 abc', strict: 'yes' },
    })
    expect(parseKasWorkflowCommand('/workflow-run solo')).toEqual({ verb: 'run', recipe: 'solo', inputs: {} })
  })
})

describe('resolveRunTarget (gap 15)', () => {
  it('prefers an explicit id, else the single live run', () => {
    expect(resolveRunTarget('wf_x', [])).toEqual({ ok: true, runId: 'wf_x' })
    expect(resolveRunTarget(null, [run('wf_1', 'running'), run('wf_2', 'completed')])).toEqual({ ok: true, runId: 'wf_1' })
    expect(resolveRunTarget(null, [run('wf_1', 'completed')])).toEqual({ ok: false, reason: 'none' })
    expect(resolveRunTarget(null, [run('wf_1', 'running'), run('wf_2', 'paused')])).toEqual({ ok: false, reason: 'ambiguous' })
  })
})

describe('runKasWorkflowCommand (gap 15)', () => {
  const recipe = (over: Partial<KasRecipe> = {}): KasRecipe =>
    ({ name: 'code-review', source: 'bundled://code-review', inputs: [], validationError: null, ...over }) as unknown as KasRecipe

  it('status opens the Workflows view and never controls a run', async () => {
    const openWorkflows = vi.fn()
    const control = vi.fn()
    const ctx = { slot: 's1', listRuns: async () => [], control, resolveRecipe: async () => null, launch: vi.fn(), openWorkflows }
    expect(await runKasWorkflowCommand({ verb: 'status' }, ctx, reasons)).toEqual({})
    expect(openWorkflows).toHaveBeenCalledTimes(1)
    expect(control).not.toHaveBeenCalled()
  })

  it('run with complete inputs launches the resolved recipe with its source and those inputs', async () => {
    const launch = vi.fn().mockResolvedValue(undefined)
    const openWorkflows = vi.fn()
    const r = recipe({ source: 'bundled://code-review', inputs: [{ name: 'pr', type: 'string' }] })
    const ctx = { slot: 's1', listRuns: async () => [], control: vi.fn(), resolveRecipe: async () => r, launch, openWorkflows }
    expect(await runKasWorkflowCommand({ verb: 'run', recipe: 'code-review', inputs: { pr: '123' } }, ctx, reasons)).toEqual({})
    expect(launch).toHaveBeenCalledWith(r, { pr: '123' })
    expect(launch.mock.calls[0][0].source).toBe('bundled://code-review')
    expect(openWorkflows).not.toHaveBeenCalled()
  })

  it('run with no declared inputs launches straight away', async () => {
    const launch = vi.fn().mockResolvedValue(undefined)
    const r = recipe({ inputs: [] })
    const ctx = { slot: 's1', listRuns: async () => [], control: vi.fn(), resolveRecipe: async () => r, launch, openWorkflows: vi.fn() }
    await runKasWorkflowCommand({ verb: 'run', recipe: 'code-review', inputs: {} }, ctx, reasons)
    expect(launch).toHaveBeenCalledTimes(1)
  })

  it('run opens the form (does not launch) when a declared input is missing', async () => {
    const launch = vi.fn()
    const openWorkflows = vi.fn()
    const r = recipe({ inputs: [{ name: 'pr', type: 'string' }] })
    const ctx = { slot: 's1', listRuns: async () => [], control: vi.fn(), resolveRecipe: async () => r, launch, openWorkflows }
    await runKasWorkflowCommand({ verb: 'run', recipe: 'code-review', inputs: {} }, ctx, reasons)
    expect(launch).not.toHaveBeenCalled()
    expect(openWorkflows).toHaveBeenCalledTimes(1)
  })

  it('run opens the form when the recipe is unknown or invalid', async () => {
    const launch = vi.fn()
    const openWorkflows = vi.fn()
    const base = { slot: 's1', listRuns: async () => [], control: vi.fn(), launch, openWorkflows }
    await runKasWorkflowCommand({ verb: 'run', recipe: 'nope', inputs: {} }, { ...base, resolveRecipe: async () => null }, reasons)
    await runKasWorkflowCommand({ verb: 'run', recipe: 'bad', inputs: {} }, { ...base, resolveRecipe: async () => recipe({ validationError: 'broken' }) }, reasons)
    expect(launch).not.toHaveBeenCalled()
    expect(openWorkflows).toHaveBeenCalledTimes(2)
  })

  it('resume/cancel control the resolved run', async () => {
    const control = vi.fn().mockResolvedValue(undefined)
    const ctx = { slot: 's1', listRuns: async () => [run('wf_1', 'paused')], control, resolveRecipe: async () => null, launch: vi.fn(), openWorkflows: vi.fn() }
    expect(await runKasWorkflowCommand({ verb: 'resume', runId: null }, ctx, reasons)).toEqual({})
    expect(control).toHaveBeenCalledWith('wf_1', 'resume')
  })

  it('fails with a reason when there is no slot / no run / it is ambiguous', async () => {
    const control = vi.fn()
    const base = { control, resolveRecipe: async () => null, launch: vi.fn(), openWorkflows: vi.fn() }
    expect(await runKasWorkflowCommand({ verb: 'resume', runId: null }, { ...base, slot: null, listRuns: async () => [] }, reasons)).toEqual({ failed: true, error: 'NO_SLOT' })
    expect(await runKasWorkflowCommand({ verb: 'cancel', runId: null }, { ...base, slot: 's1', listRuns: async () => [] }, reasons)).toEqual({ failed: true, error: 'NO_RUN' })
    expect(await runKasWorkflowCommand({ verb: 'cancel', runId: null }, { ...base, slot: 's1', listRuns: async () => [run('a', 'running'), run('b', 'running')] }, reasons)).toEqual({ failed: true, error: 'AMBIGUOUS' })
    expect(control).not.toHaveBeenCalled()
  })
})
