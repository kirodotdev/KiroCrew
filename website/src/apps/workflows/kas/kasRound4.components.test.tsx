import { describe, expect, it } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'

import { renderWithProviders } from '../../../test/helpers'
import { kasControls, kasRow } from '../../../test/kasFixtures'
import type { ChatMessage } from '../../../types'
import type { KasRunNode as ClientNode } from '../../../api/client'
import { KasEventMarkerRow, parseKasEventMarker } from './KasTurnMarker'
import { KasWorkflowToolLine, kasWorkflowToolKind } from './KasWorkflowToolLine'
import { KasStepsTree } from './KasStepsTree'
import { KasRunCardCompact } from './KasInlineRunCard'
import { isTerminalRun } from './kasModel'

const msg = (over: Partial<ChatMessage>): ChatMessage => ({ role: 'tool', content: '', cls: '', ...over }) as ChatMessage

const node = (over: Partial<ClientNode> & Pick<ClientNode, 'id'>): ClientNode => ({
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

// ── gap 10: run-end / step-note marker rows ──
describe('gap 10 — kas_event marker rows', () => {
  it('parses an outcome and a note, ignoring everything else', () => {
    expect(parseKasEventMarker(msg({ meta: { kas_event: { type: 'outcome', name: 'triage', status: 'failed', durationSecs: 76 } } }))).toEqual({
      type: 'outcome',
      name: 'triage',
      status: 'failed',
      durationSecs: 76,
    })
    expect(parseKasEventMarker(msg({ meta: { kas_event: { type: 'note', name: 'triage', severity: 'warning', message: 'need input', step: 'Review', agent: 'rev' } } }))).toEqual({
      type: 'note',
      name: 'triage',
      severity: 'warning',
      message: 'need input',
      step: 'Review',
      agent: 'rev',
    })
    expect(parseKasEventMarker(msg({ meta: {} }))).toBeNull()
    expect(parseKasEventMarker(msg({}))).toBeNull()
  })

  it('renders a failed outcome and a warning note on the right branch', () => {
    // Assert the deterministic branch (data attributes), not the interpolated English.
    const { unmount } = renderWithProviders(<KasEventMarkerRow marker={{ type: 'outcome', name: 'triage', status: 'failed', durationSecs: 76 }} />)
    const row = screen.getByTestId('kas-event-marker')
    expect(row).toHaveAttribute('data-kind', 'outcome')
    expect(row).toHaveAttribute('data-status', 'failed')
    unmount()
    renderWithProviders(<KasEventMarkerRow marker={{ type: 'note', name: 'triage', severity: 'warning', message: 'need input', step: 'Review', agent: 'rev' }} />)
    const note = screen.getByTestId('kas-event-marker')
    expect(note).toHaveAttribute('data-kind', 'note')
    expect(note).toHaveAttribute('data-severity', 'warning')
  })

  it('a warning step-note renders the INCOMING card from the step with a Needs you pill', () => {
    renderWithProviders(<KasEventMarkerRow marker={{ type: 'note', name: 'triage', severity: 'warning', message: 'need input', step: 'Review', agent: 'rev' }} />)
    const card = screen.getByTestId('session-message-card')
    expect(card).toHaveAttribute('data-direction', 'incoming')
    // Avatar + title name the step; the agent and run show as meta.
    expect(screen.getByTestId('session-card-title').textContent).toBe('Review')
    expect(screen.getByTestId('session-card-meta').textContent).toBe('rev · triage')
    expect(screen.getByTestId('session-card-needs-you')).toBeInTheDocument()
  })

  it('a plain (info) step-note is the card without the pill; an error note keeps the one-line row', () => {
    const { unmount } = renderWithProviders(<KasEventMarkerRow marker={{ type: 'note', name: 'triage', severity: 'info', message: 'done scanning', step: 'Scan', agent: 'rev' }} />)
    expect(screen.getByTestId('session-message-card')).toBeInTheDocument()
    expect(screen.queryByTestId('session-card-needs-you')).not.toBeInTheDocument()
    unmount()
    renderWithProviders(<KasEventMarkerRow marker={{ type: 'note', name: 'triage', severity: 'error', message: 'it broke', step: 'Fix', agent: 'rev' }} />)
    // Error keeps the red one-line row, not a card, so its failure cue survives.
    expect(screen.queryByTestId('session-message-card')).not.toBeInTheDocument()
    const row = screen.getByTestId('kas-event-marker')
    expect(row).toHaveAttribute('data-severity', 'error')
  })
})

// ── gap 26: other workflow tool calls ──
describe('gap 26 — workflow tool line', () => {
  it('classifies KAS workflow tools by title and leaves run_workflow / plain tools alone', () => {
    expect(kasWorkflowToolKind(msg({ content: '🔧 Validate Workflow' }))).toBe('validate')
    expect(kasWorkflowToolKind(msg({ content: '🔧 Save Workflow Definition' }))).toBe('save')
    expect(kasWorkflowToolKind(msg({ content: '🔧 Inspect Workflow' }))).toBe('inspect')
    expect(kasWorkflowToolKind(msg({ content: '🔧 Run Workflow' }))).toBeNull()
    expect(kasWorkflowToolKind(msg({ content: '🔧 Execute Bash' }))).toBeNull()
    expect(kasWorkflowToolKind(msg({ content: '🔧 Validate Workflow', role: 'assistant' }))).toBeNull()
  })

  it('shows an invalid verdict and an Open-run link from the output', () => {
    renderWithProviders(
      <KasWorkflowToolLine
        message={msg({ content: '🔧 Validate Workflow', meta: { output: JSON.stringify({ valid: false, errors: ['bad'], workflowId: 'wf_abc123de' }) } })}
        kind="validate"
        fallback={<p>generic</p>}
      />,
    )
    expect(screen.getByTestId('kas-tool-invalid')).toBeInTheDocument()
    expect(screen.getByTestId('kas-tool-open-run')).toBeInTheDocument()
    expect(screen.queryByText('generic')).toBeNull()
  })

  it('falls back to the generic line with nothing richer to say', () => {
    renderWithProviders(<KasWorkflowToolLine message={msg({ content: '🔧 Inspect Workflow', meta: {} })} kind="inspect" fallback={<p>generic</p>} />)
    expect(screen.getByText('generic')).toBeInTheDocument()
  })
})

// ── gaps 1 / 11 / 23: step-tree polish ──
describe('gaps 1/11/23 — step tree', () => {
  const treeRow = () =>
    kasRow({
      queuedSteps: [{ id: 'q1', type: 'step', agent: null, label: 'Queued A', maxIterations: null, children: [] }],
      nodes: [
        node({ id: 'ret', label: 'Build', status: 'running', retryWait: { reason: 'rate limited', since: 't' } }),
        node({ id: 'apr', label: 'Deploy', status: 'running', waitingFor: 'approval' }),
        node({ id: 'done', label: 'Plan', status: 'completed', startedAt: '2026-10-16T10:00:00Z', endedAt: '2026-10-16T10:01:00Z' }),
        node({ id: 'next', label: 'Ship', status: 'pending', replacementPending: true }),
        node({ id: 'w', label: 'Watch CI', type: 'watch', status: 'running', watchOutcome: { outcome: 'new-activity', at: null } }),
      ],
    })

  it('renders retry-wait, a needs-approval tag, a watch outcome, per-step duration, will-be-replaced and queued rows', () => {
    renderWithProviders(<KasStepsTree nodes={treeRow().nodes} selectedPath={null} onSelect={() => {}} row={treeRow()} />)
    expect(screen.getByTestId('kas-step-retry-wait')).toBeInTheDocument()
    expect(screen.getByTestId('kas-step-wait')).toBeInTheDocument()
    expect(screen.getByTestId('kas-watch-outcome')).toBeInTheDocument()
    expect(screen.getByTestId('kas-step-duration')).toHaveTextContent('1m')
    expect(screen.getByTestId('kas-will-replace')).toBeInTheDocument()
    expect(screen.getByTestId('kas-queued-steps')).toBeInTheDocument()
    expect(screen.getByTestId('kas-queued-step')).toHaveTextContent('Queued A')
  })

  it('labels the watch marker "Watches for activity" with a matching tooltip (CX r8)', () => {
    renderWithProviders(<KasStepsTree nodes={treeRow().nodes} selectedPath={null} onSelect={() => {}} row={treeRow()} />)
    const marker = screen.getByTestId('kas-watch-marker')
    expect(marker).toHaveTextContent('Watches for activity')
    expect(marker).toHaveAttribute('title', 'Watches for activity')
  })

  it('a collapsed group summarises what it holds', () => {
    const row = kasRow({
      nodes: [
        {
          ...node({ id: 'grp', label: 'Checks', type: 'sequence', status: 'running' }),
          children: [node({ id: 'c1', label: 'Lint', waitingFor: 'input' }), node({ id: 'c2', label: 'Test' })],
        },
      ],
    })
    renderWithProviders(<KasStepsTree nodes={row.nodes} selectedPath={null} onSelect={() => {}} row={row} />)
    // Groups start open; collapse it to reveal the summary.
    fireEvent.click(screen.getByTestId('kas-step-group'))
    expect(screen.getByTestId('kas-group-summary')).toBeInTheDocument()
  })
})

// ── gap 14: the chat card embeds the step tree ──
describe('gap 14 — step tree in the chat card', () => {
  it('shows the tree expanded for a live run and collapsed for a finished one, with a toggle', () => {
    const { unmount } = renderWithProviders(<KasRunCardCompact row={kasRow()} approvals={[]} />)
    expect(screen.getByTestId('kas-inline-tree')).toBeInTheDocument()
    expect(screen.getByTestId('kas-steps-tree')).toBeInTheDocument()
    unmount()
    const done = kasRow({ status: 'completed', controls: kasControls({ display: 'completed', primary: null, canStop: false }) })
    expect(isTerminalRun(done)).toBe(true)
    renderWithProviders(<KasRunCardCompact row={done} approvals={[]} />)
    // Toggle present, tree collapsed by default on a finished run; a click opens it.
    expect(screen.getByTestId('kas-inline-tree-toggle')).toBeInTheDocument()
    expect(screen.queryByTestId('kas-steps-tree')).toBeNull()
    fireEvent.click(screen.getByTestId('kas-inline-tree-toggle'))
    expect(screen.getByTestId('kas-steps-tree')).toBeInTheDocument()
  })
})
