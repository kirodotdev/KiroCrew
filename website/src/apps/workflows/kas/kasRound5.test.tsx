/**
 * Round 5 review fixes.
 *
 * Gap 25: a Stop confirm withdraws itself when the run (or step) it would stop
 * ends while the dialog is open, and a Delete confirm on the same surface is
 * never withdrawn by that.
 *
 * Gap 22 on the run list: Customize › Workflows orders runs like the chat panel
 * (live first, by creation), not by the last event.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'

import { api } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { renderWithProviders } from '../../../test/helpers'
import { kasControls, kasRow } from '../../../test/kasFixtures'
import { KasRunIconControls } from './KasRunBits'
import { KasRunCard, KasRunList } from './KasRunView'
import { KasTreeStepControls } from './KasParityControls'
import { kasWorkflowToolKind } from './KasWorkflowToolLine'
import { extractKasWorkflowId } from './KasInlineRunCard'
import type { ChatMessage } from '../../../types'

let control: ReturnType<typeof vi.spyOn>
let step: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  control = vi.spyOn(api, 'kasRunControl').mockResolvedValue({ workflowId: 'wf-1', row: null })
  step = vi.spyOn(api, 'kasStepAction').mockResolvedValue({} as never)
})

afterEach(() => {
  vi.restoreAllMocks()
})

const running = () => kasRow()
const finished = () =>
  kasRow({
    status: 'completed',
    endedAt: '2026-10-16T10:09:00Z',
    activeSteps: [],
    controls: kasControls({ display: 'completed', primary: null, canStop: false, canDelete: true }),
  })

const confirmStop = () => screen.queryByRole('button', { name: i18nT('apps.workflows.kas.stop_run') })

describe('gap 25 — Stop confirm withdraws when the run ends', () => {
  it('icon controls: the open Stop dialog closes and nothing is cancelled', async () => {
    const { rerender } = renderWithProviders(<KasRunIconControls row={running()} />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.kas.stop') }))
    expect(confirmStop()).not.toBeNull()
    rerender(<KasRunIconControls row={finished()} />)
    await waitFor(() => expect(confirmStop()).toBeNull())
    expect(control).not.toHaveBeenCalled()
  })

  it('icon controls: a Delete confirm on a finished run stays open', async () => {
    const row = finished()
    const { rerender } = renderWithProviders(<KasRunIconControls row={row} withDelete />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.kas.delete') }))
    const confirmDelete = () => screen.queryByRole('button', { name: i18nT('apps.workflows.kas.delete_run') })
    expect(confirmDelete()).not.toBeNull()
    rerender(<KasRunIconControls row={{ ...row, updatedAt: '2026-10-16T10:10:00Z' }} withDelete />)
    await act(async () => {})
    expect(confirmDelete()).not.toBeNull()
  })

  it('icon controls: a run still running keeps the dialog, and a yes cancels it', async () => {
    const { rerender } = renderWithProviders(<KasRunIconControls row={running()} />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.workflows.kas.stop') }))
    rerender(<KasRunIconControls row={{ ...running(), completedSteps: 2 }} />)
    await act(async () => {})
    const yes = confirmStop()
    expect(yes).not.toBeNull()
    fireEvent.click(yes!)
    await waitFor(() => expect(control).toHaveBeenCalledWith('wf-1', 'cancel'))
  })

  it('run card: the open Stop dialog closes when the run ends', async () => {
    const { rerender } = renderWithProviders(<KasRunCard row={running()} approvals={[]} />)
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('apps.workflows.kas.stop'), 'i') }))
    expect(confirmStop()).not.toBeNull()
    rerender(<KasRunCard row={finished()} approvals={[]} />)
    await waitFor(() => expect(confirmStop()).toBeNull())
    expect(control).not.toHaveBeenCalled()
  })

  it('step controls: the open Stop-step dialog closes when the step ends', async () => {
    const row = running()
    const live = { ...row.nodes[1], status: 'running', actions: ['cancel'] } as (typeof row.nodes)[number]
    const done = { ...live, status: 'completed', actions: [] } as (typeof row.nodes)[number]
    const { rerender } = renderWithProviders(<KasTreeStepControls row={row} node={live} />)
    fireEvent.click(screen.getByTestId('kas-stop-step'))
    const confirmStep = () => screen.queryByRole('button', { name: i18nT('apps.workflows.kas.stop_step') })
    expect(confirmStep()).not.toBeNull()
    rerender(<KasTreeStepControls row={row} node={done} />)
    await waitFor(() => expect(confirmStep()).toBeNull())
    expect(step).not.toHaveBeenCalled()
  })
})

describe('gap 22 — the run list keeps the chat panel order', () => {
  it('a live run stays above a finished one that saw a later event, and an event does not move it', () => {
    const live = kasRow({ workflowId: 'live', name: 'Live run', createdAt: '2026-10-16T08:00:00Z', updatedAt: '2026-10-16T08:01:00Z' })
    const done = kasRow({
      workflowId: 'done',
      name: 'Done run',
      status: 'completed',
      createdAt: '2026-10-16T09:00:00Z',
      endedAt: '2026-10-16T09:30:00Z',
      updatedAt: '2026-10-16T11:00:00Z',
      activeSteps: [],
      controls: kasControls({ display: 'completed', primary: null, canStop: false, canDelete: true }),
    })
    renderWithProviders(<KasRunList runs={[done, live]} approvals={[]} emptyText="none" />)
    const list = screen.getByRole('list', { name: i18nT('apps.workflows.kas.runs') })
    const titles = Array.from(list.children).map(li => li.textContent ?? '')
    expect(titles[0]).toContain('Live run')
    expect(titles[1]).toContain('Done run')
  })
})

describe('gap 26 — an MCP-served lookalike is never drawn as a KAS workflow tool', () => {
  const tool = (over: Partial<ChatMessage>): ChatMessage => ({ role: 'tool', content: '', cls: '', ...over }) as ChatMessage
  it('a GitHub-style "Check Workflow" or "Run Workflow" MCP call keeps the generic line', () => {
    const output = JSON.stringify({ workflowId: 'wf_0123abcd', status: 'running' })
    expect(kasWorkflowToolKind(tool({ content: '🔧 Check Workflow Status', meta: { mcp_server: 'github' } }))).toBeNull()
    expect(extractKasWorkflowId(tool({ content: '🔧 Run Workflow', meta: { mcp_server: 'github', output } }))).toBeNull()
  })
  it('the same rows with no MCP identity still classify as KAS tools', () => {
    const output = JSON.stringify({ workflowId: 'wf_0123abcd', status: 'running' })
    expect(kasWorkflowToolKind(tool({ content: '🔧 Check Workflow Status', meta: {} }))).toBe('inspect')
    expect(extractKasWorkflowId(tool({ content: '🔧 Run Workflow', meta: { output } }))).toBe('wf_0123abcd')
  })
})
