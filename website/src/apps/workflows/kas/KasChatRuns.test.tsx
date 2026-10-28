import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../../../api/client'
import { renderWithProviders } from '../../../test/helpers'
import { kasControls, kasRow, kasTranscript } from '../../../test/kasFixtures'
import { KasChatRuns, runsOfChat } from './KasChatRuns'
import { getKasSelection, resetKasSelections, setKasSelection } from './kasSelection'

beforeEach(() => {
  resetKasSelections()
  // The ledger read-back is empty unless a test says otherwise.
  vi.spyOn(api, 'kasChatRuns').mockResolvedValue({ runs: [], approvals: [], revision: 0 })
})

afterEach(() => {
  vi.restoreAllMocks()
})

const crew = (hasKasRuns: boolean) => <p data-testid="crew">{hasKasRuns ? 'crew-with-kas' : 'crew-alone'}</p>

const finished = (i: number) =>
  kasRow({
    workflowId: `done-${i}`,
    name: `Done ${i}`,
    slotKey: 'chat-1',
    status: 'completed',
    updatedAt: `2026-10-15T10:${String(i).padStart(2, '0')}:00Z`,
    controls: kasControls({ display: 'completed', primary: null, canStop: false, canDelete: true }),
  })

describe('runsOfChat', () => {
  it('keeps only the runs this chat parents', () => {
    const runs = [kasRow({ workflowId: 'a', slotKey: 'chat-1' }), kasRow({ workflowId: 'b', slotKey: 'chat-2' }), kasRow({ workflowId: 'c', slotKey: null })]
    expect(runsOfChat(runs, 'chat-1').map(r => r.workflowId)).toEqual(['a'])
    expect(runsOfChat(runs, '')).toEqual([])
  })
})

describe('KasChatRuns — list', () => {
  it('expands a live run to its steps with agent and model · effort, and opens a step chat', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    const node = {
      id: 'investigate', type: 'step', agent: 'wf-planner', label: 'investigate', maxIterations: null,
      status: 'running', sessionId: 'sess-1', iteration: null, reason: null,
      model: 'claude-opus-5', effort: 'high', children: [],
    }
    vi.spyOn(api, 'kasState').mockResolvedValue({
      runs: [kasRow({ workflowId: 'live', slotKey: 'chat-1', name: 'investigate', nodes: [node] })],
      approvals: [],
      revision: 1,
    })
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    const toggle = await screen.findByTestId('kas-panel-row-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('kas-step-meta')).toHaveTextContent('wf-planner')
    expect(screen.getByTestId('kas-step-meta')).toHaveTextContent('claude-opus-5 · high')
    fireEvent.click(toggle)
    expect(screen.queryByTestId('kas-tree-step')).toBeNull()
    fireEvent.click(toggle)
    fireEvent.click(screen.getByTestId('kas-tree-step'))
    expect(getKasSelection('chat-1')).toEqual({ workflowId: 'live', nodeId: 'investigate' })
  })
  it('shows a run from before a gateway restart, read back from the ledger', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [], approvals: [], revision: 3 })
    const ledger = vi.spyOn(api, 'kasChatRuns').mockResolvedValue({ runs: [finished(1)], approvals: [], revision: 3 })
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    expect(await screen.findByTestId('kas-panel-row')).toHaveTextContent('Done 1')
    expect(ledger).toHaveBeenCalledWith('chat-1')
  })
  it("shows the chat's KAS runs as compact rows above the Crew content", async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({
      runs: [kasRow({ workflowId: 'mine', slotKey: 'chat-1', name: 'Mine' }), kasRow({ workflowId: 'other', slotKey: 'chat-2', name: 'Other' })],
      approvals: [],
      revision: 1,
    })
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    await waitFor(() => expect(screen.getByTestId('kas-chat-runs')).toBeInTheDocument())
    const row = screen.getByTestId('kas-panel-row')
    expect(row).toHaveTextContent('Mine')
    expect(within(row).getByRole('button', { name: /^pause$/i })).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /^stop$/i })).toBeInTheDocument()
    expect(screen.queryByText('Other')).toBeNull()
    expect(screen.getByTestId('crew')).toHaveTextContent('crew-with-kas')
  })

  it('lists every live run, then only the 10 newest finished ones; attention shows a warning, not a spinner', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    const blocked = kasRow({
      workflowId: 'blocked',
      slotKey: 'chat-1',
      updatedAt: '2026-10-14T00:00:00Z',
      blocking: { nodeId: 'review', label: 'Review', kind: 'permission', sessionId: null, toolCallId: null },
      controls: kasControls({ needsAttention: true }),
    })
    vi.spyOn(api, 'kasState').mockResolvedValue({
      runs: [...Array.from({ length: 12 }, (_, i) => finished(i)), blocked],
      approvals: [],
      revision: 1,
    })
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    await waitFor(() => expect(screen.getAllByTestId('kas-panel-row')).toHaveLength(11))
    const ids = screen.getAllByTestId('kas-panel-row').map(r => r.getAttribute('data-workflow-id'))
    expect(ids[0]).toBe('blocked')
    expect(ids.slice(1)).toEqual(['done-11', 'done-10', 'done-9', 'done-8', 'done-7', 'done-6', 'done-5', 'done-4', 'done-3', 'done-2'])
    const first = screen.getAllByTestId('kas-panel-row')[0]
    expect(first.querySelector('[data-icon="warning"]')).not.toBeNull()
    expect(first.querySelector('[data-icon="running"]')).toBeNull()
    // A finished row offers no inline controls.
    expect(within(screen.getAllByTestId('kas-panel-row')[1]).queryByRole('button', { name: /^stop$/i })).toBeNull()
  })

  it('a row opens the run detail; Back returns to the list', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [kasRow({ slotKey: 'chat-1' })], approvals: [], revision: 1 })
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    fireEvent.click(await screen.findByRole('button', { name: /open review and fix/i }))
    expect(await screen.findByTestId('kas-run-detail')).toHaveAttribute('data-workflow-id', 'wf-1')
    expect(getKasSelection('chat-1')).toEqual({ workflowId: 'wf-1' })
    expect(screen.queryByTestId('crew')).toBeNull()
    fireEvent.click(screen.getByTestId('kas-detail-back'))
    expect(await screen.findByTestId('kas-chat-runs')).toBeInTheDocument()
    expect(getKasSelection('chat-1')).toBeNull()
  })

  it('opens straight on the run the in-chat card selected', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [kasRow({ slotKey: 'chat-1' })], approvals: [], revision: 1 })
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    setKasSelection('chat-1', { workflowId: 'wf-1' })
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    expect(await screen.findByTestId('kas-run-detail')).toBeInTheDocument()
  })

  it('leaves the Crew content alone off a KAS backend', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'crew', backend: '' })
    const state = vi.spyOn(api, 'kasState')
    renderWithProviders(<KasChatRuns slotKey="chat-1">{crew}</KasChatRuns>)
    await waitFor(() => expect(api.workflowEngine).toHaveBeenCalled())
    expect(screen.getByTestId('crew')).toHaveTextContent('crew-alone')
    expect(screen.queryByTestId('kas-chat-runs')).toBeNull()
    expect(state).not.toHaveBeenCalled()
  })
})
