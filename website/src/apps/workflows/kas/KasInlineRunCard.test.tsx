import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../../../api/client'
import type { ChatMessage } from '../../../types'
import { createTestStore, renderWithProviders } from '../../../test/helpers'
import { kasApproval, kasControls, kasRow } from '../../../test/kasFixtures'
import { SlotProvider } from '../../../providers/SlotContext'
import { i18nT } from '../../../i18n/t'
import { KasInlineRunCard, KasRunCardCompact, extractKasWorkflowId, isKasRunWorkflowTool, planMetaLine, runFooterLine } from './KasInlineRunCard'
import { getKasSelection, resetKasSelections } from './kasSelection'

// The exact message the dev gateway persisted for a chat's run_workflow call.
const launch = (output: unknown, content = '🔧 Run Workflow'): ChatMessage =>
  ({
    role: 'tool',
    content,
    meta: { output: typeof output === 'string' ? output : JSON.stringify(output) },
  }) as unknown as ChatMessage

const STARTED = {
  message: "Workflow 'wf_4d59ddebfbc27732' started successfully. Status: running.",
  workflowId: 'wf_4d59ddebfbc27732',
  status: 'running',
}

beforeEach(() => {
  resetKasSelections()
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('extractKasWorkflowId', () => {
  it("reads KAS's run_workflow answer, and nothing else", () => {
    expect(extractKasWorkflowId(launch(STARTED))).toBe('wf_4d59ddebfbc27732')
    expect(isKasRunWorkflowTool(launch(STARTED))).toBe(true)
    // Crew's own workflow_run answers in prose with a different id shape.
    expect(extractKasWorkflowId(launch('Started workflow run `wf_000003` (name: x).'))).toBeNull()
    expect(extractKasWorkflowId(launch({ ...STARTED, workflowId: 'wf_000003' }))).toBeNull()
    expect(extractKasWorkflowId(launch({ workflowId: STARTED.workflowId }))).toBeNull()
    expect(extractKasWorkflowId(launch(STARTED, 'Run Workflow'))).toBeNull()
    expect(extractKasWorkflowId({ ...launch(STARTED), role: 'assistant' } as ChatMessage)).toBeNull()
    expect(extractKasWorkflowId(launch('{not json'))).toBeNull()
  })
})

const step = (id: string, label: string) => ({ ...kasRow().nodes[0], id, label, children: [] })

describe('planMetaLine / runFooterLine', () => {
  it('names the top-level steps, at most four then …', () => {
    expect(planMetaLine(kasRow())).toBe('2-step workflow · Plan → Fix loop')
    const five = kasRow({ nodes: ['A', 'B', 'C', 'D', 'E'].map(l => step(l, l)) })
    expect(planMetaLine(five)).toBe('5-step workflow · A → B → C → D → …')
    expect(planMetaLine(kasRow({ nodes: [] }))).toBeNull()
  })

  it('prepends the start stamp, shows Step n/m and elapsed while live; drops Step n/m when unknown or finished, never 0/0', () => {
    const now = Date.parse('2026-10-16T10:01:05Z')
    const live = runFooterLine(kasRow(), now)
    // Gap 24: a start stamp is prepended; the Step/elapsed tail is unchanged.
    expect(live?.endsWith('Step 1/3 · 1m 4s')).toBe(true)
    expect(live).not.toBe('Step 1/3 · 1m 4s')
    const noSteps = runFooterLine(kasRow({ completedSteps: 0, totalSteps: 0 }), now)
    expect(noSteps?.endsWith('1m 4s')).toBe(true)
    expect(noSteps).not.toContain('Step ')
    const done = kasRow({ status: 'completed', endedAt: '2026-10-16T10:00:31Z', controls: kasControls({ display: 'completed', primary: null, canStop: false }) })
    expect(runFooterLine(done, now)?.endsWith('30s')).toBe(true)
    expect(runFooterLine(kasRow({ startedAt: null, createdAt: null, completedSteps: null }), now)).toBeNull()
  })
})

describe('KasRunCardCompact', () => {
  it('draws title, state, meta, the active step with its agent, and the footer', () => {
    renderWithProviders(<KasRunCardCompact row={kasRow()} approvals={[]} />)
    const card = screen.getByTestId('kas-inline-card')
    expect(within(card).getByText('Review and fix')).toBeInTheDocument()
    expect(screen.getByTestId('kas-run-state')).toHaveTextContent('Running')
    expect(screen.getByTestId('kas-inline-meta')).toHaveTextContent('2-step workflow · Plan → Fix loop')
    const status = screen.getByTestId('kas-inline-status')
    expect(status).toHaveTextContent('Review')
    expect(within(status).getByTestId('kas-agent-tag')).toHaveTextContent('reviewer')
    expect(screen.getByTestId('kas-inline-footer')).toHaveTextContent(/Step 1\/3 · /)
    expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^stop$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /open chat/i })).toBeNull()
  })

  it('a blocked run says which step waits and for what, with its approvals under the card', () => {
    const row = kasRow({
      blocking: { nodeId: 'review', label: 'Review', kind: 'permission', sessionId: 's-review', toolCallId: 'tc' },
      controls: kasControls({ primary: 'open', needsAttention: true }),
    })
    renderWithProviders(<KasRunCardCompact row={row} approvals={[kasApproval()]} />)
    expect(screen.getByTestId('kas-inline-status')).toHaveTextContent('Review — Waiting for approval')
    // Gap 7: a running run blocked on an approval reads "Needs you", not "Paused".
    expect(screen.getByTestId('kas-run-state')).toHaveTextContent(i18nT('apps.workflows.kas.state_needs_you'))
    expect(screen.getByTestId('kas-approval')).toBeInTheDocument()
    // `open` is the primary, but we are in the chat: no Respond button, just Stop.
    expect(screen.queryByRole('button', { name: /respond in chat/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^pause$/i })).toBeNull()
    expect(screen.getByRole('button', { name: /^stop$/i })).toBeInTheDocument()
  })

  it('a question reads "Waiting for an answer"; a paused run shows its reason', () => {
    const { unmount } = renderWithProviders(
      <KasRunCardCompact row={kasRow({ blocking: { nodeId: 'review', label: 'Review', kind: 'question', sessionId: null, toolCallId: null } })} approvals={[]} />,
    )
    expect(screen.getByTestId('kas-inline-status')).toHaveTextContent('Review — Waiting for an answer')
    unmount()
    renderWithProviders(
      <KasRunCardCompact
        row={kasRow({ status: 'paused', activeSteps: [], pauseReason: 'Paused by you', controls: kasControls({ display: 'paused', primary: 'resume' }) })}
        approvals={[]}
      />,
    )
    expect(screen.getByTestId('kas-inline-status')).toHaveTextContent('Paused by you')
    expect(screen.getByRole('button', { name: /^resume$/i })).toBeInTheDocument()
  })

  it('Stop is confirmed before anything is sent', async () => {
    const control = vi.spyOn(api, 'kasRunControl').mockResolvedValue({ workflowId: 'wf-1', row: null })
    renderWithProviders(<KasRunCardCompact row={kasRow()} approvals={[]} />)
    fireEvent.click(screen.getByRole('button', { name: /^stop$/i }))
    const dialog = await screen.findByRole('dialog')
    expect(control).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: /stop run/i }))
    await waitFor(() => expect(control).toHaveBeenCalledWith('wf-1', 'cancel'))
  })

  it('clicking the card selects this run for the chat and opens the Workflows tab', () => {
    const store = createTestStore()
    renderWithProviders(<KasRunCardCompact row={kasRow({ slotKey: 'chat-abc' })} approvals={[]} />, { store })
    fireEvent.click(screen.getByTestId('kas-inline-open'))
    expect(getKasSelection(store.getState().chat.activeSlot ?? 'chat-abc')).toEqual({ workflowId: 'wf-1' })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('workflows')
  })

  it('the controls do not open the panel', async () => {
    vi.spyOn(api, 'kasRunControl').mockResolvedValue({ workflowId: 'wf-1', row: null })
    const store = createTestStore()
    renderWithProviders(<KasRunCardCompact row={kasRow({ slotKey: 'chat-abc' })} approvals={[]} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^pause$/i }))
    expect(getKasSelection('chat-abc')).toBeNull()
    expect(store.getState().chat.activityTab).not.toBe('workflows')
  })
})

describe('KasInlineRunCard', () => {
  it('draws the live run as the compact card', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({
      runs: [kasRow({ workflowId: STARTED.workflowId, slotKey: 'chat-1' })],
      approvals: [kasApproval({ workflowId: STARTED.workflowId })],
      revision: 1,
    })
    renderWithProviders(<KasInlineRunCard workflowId={STARTED.workflowId} fallback={<p>generic</p>} />)
    const card = await screen.findByTestId('kas-inline-card')
    expect(card).toHaveAttribute('data-workflow-id', STARTED.workflowId)
    expect(screen.getByTestId('kas-approval')).toBeInTheDocument()
    expect(screen.queryByText('generic')).toBeNull()
  })

  it('points at the panel for a run neither the live list nor the ledger holds', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [], approvals: [], revision: 1 })
    vi.spyOn(api, 'kasChatRuns').mockResolvedValue({ runs: [], approvals: [], revision: 1 })
    renderWithProviders(<KasInlineRunCard workflowId={STARTED.workflowId} fallback={<p>generic</p>} />)
    // Gap 24: the placeholder reads "Starting workflow", not the raw wf_ id.
    const placeholder = await screen.findByTestId('kas-inline-run-placeholder')
    expect(placeholder).not.toHaveTextContent(STARTED.workflowId)
    expect(screen.getByRole('button', { name: /open workflows panel/i })).toBeInTheDocument()
  })

  it('draws a run from before a gateway restart once the chat reads it back', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas' })
    vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [], approvals: [], revision: 1 })
    const ledger = vi.spyOn(api, 'kasChatRuns').mockResolvedValue({
      runs: [kasRow({ workflowId: STARTED.workflowId, slotKey: 'chat-6' })],
      approvals: [],
      revision: 1,
    })
    renderWithProviders(
      <SlotProvider slotId="chat-6">
        <KasInlineRunCard workflowId={STARTED.workflowId} fallback={<p>generic</p>} />
      </SlotProvider>,
    )
    expect(await screen.findByTestId('kas-inline-card')).toHaveAttribute('data-workflow-id', STARTED.workflowId)
    expect(ledger).toHaveBeenCalledWith('chat-6')
  })

  it('falls back to the generic tool line off a KAS backend', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'crew', backend: '' })
    renderWithProviders(<KasInlineRunCard workflowId={STARTED.workflowId} fallback={<p>generic</p>} />)
    await waitFor(() => expect(api.workflowEngine).toHaveBeenCalled())
    expect(screen.getByText('generic')).toBeInTheDocument()
    expect(screen.queryByTestId('kas-inline-card')).toBeNull()
  })

  it('shows a held run read-only when Workflows is off: view + Stop, note + settings link (gap 5)', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'crew', backend: 'kas', kas_backend: true, kas_workflows: false })
    vi.spyOn(api, 'kasState').mockResolvedValue({
      runs: [kasRow({ workflowId: STARTED.workflowId, slotKey: 'chat-1', status: 'running' })],
      approvals: [kasApproval({ workflowId: STARTED.workflowId })],
      revision: 1,
    })
    renderWithProviders(<KasInlineRunCard workflowId={STARTED.workflowId} fallback={<p>generic</p>} />)
    const card = await screen.findByTestId('kas-inline-card')
    expect(card).toHaveAttribute('data-workflow-id', STARTED.workflowId)
    expect(screen.getByTestId('kas-inline-off-note')).toBeInTheDocument()
    expect(screen.getByTestId('kas-inline-off-settings')).toBeInTheDocument()
    // Stop stays; pause/resume and the approval card are gone.
    expect(screen.getByRole('button', { name: /^stop$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^pause$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^resume$/i })).toBeNull()
    expect(screen.queryByTestId('kas-approval')).toBeNull()
    expect(screen.queryByText('generic')).toBeNull()
  })

  it('falls back to the generic line when off and no run is held (gap 5)', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'crew', backend: 'kas', kas_backend: true, kas_workflows: false })
    vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [], approvals: [], revision: 1 })
    vi.spyOn(api, 'kasChatRuns').mockResolvedValue({ runs: [], approvals: [], revision: 1 })
    renderWithProviders(<KasInlineRunCard workflowId={STARTED.workflowId} fallback={<p>generic</p>} />)
    expect(await screen.findByText('generic')).toBeInTheDocument()
    expect(screen.queryByTestId('kas-inline-card')).toBeNull()
  })
})
