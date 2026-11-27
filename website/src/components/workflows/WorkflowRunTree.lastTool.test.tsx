/**
 * The tool call a step last started (its `agent_progress` `last_tool`) is shown
 * beside a running row only; a finished row looks as it did before progress was
 * emitted.
 */
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunTree from './WorkflowRunTree'
import type { WfEvent } from './runModel'
import { i18next, initI18n } from '../../i18n/all'
import { renderWithProviders } from '../../test/helpers'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

function ev(type: string, data: Record<string, unknown>, seq: number): WfEvent {
  return { run_id: 'wf_tool', seq, ts: '2026-11-25T00:00:00.000Z', type, data }
}

const started = [
  ev('phase_started', { title: 'P' }, 0),
  ev('agent_started', { agent_id: 'a1', label: 'agent one', phase: 'P' }, 1),
  ev('agent_progress', { agent_id: 'a1', turns: 1, last_tool: 'Read a.txt', elapsed_s: 0.1 }, 2),
]

describe('WorkflowRunTree last tool', () => {
  it('shows the last started tool call beside a running step', async () => {
    await initI18n()
    const { container } = renderWithProviders(<WorkflowRunTree events={started} status="running" />)
    expect(container.querySelector('span.text-muted.truncate')?.textContent).toBe('· Read a.txt')
  })

  it('drops it once the step finished', async () => {
    await initI18n()
    const events = [...started, ev('agent_finished', { agent_id: 'a1', result_summary: 'ok', ok: true }, 3)]
    const { container } = renderWithProviders(<WorkflowRunTree events={events} status="running" />)
    expect(container.querySelector('span.text-muted.truncate')).toBeNull()
  })
})
