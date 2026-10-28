/**
 * The Kiro Agent parity controls: loop exhausted, mark done / failed, stop one
 * step, retry / run again, the chat dock, include groups, the plan tree, pause
 * reasons, the question card and "Waiting for your reply". Every button is
 * asserted to follow `run.actions` / `node.actions`, never the status alone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../../../api/client'
import { renderWithProviders } from '../../../test/helpers'
import {
  kasAwaitingRow,
  kasFailedRow,
  kasIncludeRow,
  kasLoopExhaustedRow,
  kasPlanApproval,
  kasQuestion,
  kasRow,
} from '../../../test/kasFixtures'
import { KasApprovalCard } from './KasRunView'
import { KasRunDetail } from './KasRunDetail'
import { KasStepsTree } from './KasStepsTree'
import { KasLoopExhaustedControls, KasPauseReasonLine, KasRunRecoveryButtons, KasStepDecisionButtons } from './KasParityControls'
import { KasQuestionCard } from './KasQuestionCard'
import { KasChatDockView, dockHeadline } from './KasChatDock'
import { KasDockModeSetting } from './KasWorkflowSettings'
import { KAS_DOCK_MODE_KEY, readKasDockMode } from './useKasWorkflows'
import {
  dockSummary,
  findNode,
  includeMemberLabel,
  liveStatusLine,
  parseGrantCount,
  pauseLine,
  planTreeNodes,
  stepDisplayLabel,
} from './kasModel'

let stepAction: ReturnType<typeof vi.spyOn>
let retryRun: ReturnType<typeof vi.spyOn>
let restartRun: ReturnType<typeof vi.spyOn>
let answerQuestion: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  stepAction = vi.spyOn(api, 'kasStepAction').mockResolvedValue({})
  retryRun = vi.spyOn(api, 'kasRetryRun').mockResolvedValue({})
  restartRun = vi.spyOn(api, 'kasRestartRun').mockResolvedValue({})
  answerQuestion = vi.spyOn(api, 'kasAnswerQuestion').mockResolvedValue({})
  vi.spyOn(api, 'kasRunControl').mockResolvedValue({ workflowId: 'wf-1', row: null })
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.removeItem(KAS_DOCK_MODE_KEY)
})

describe('1. loop exhausted', () => {
  it('Grant more passes sends the typed count to the loop node', async () => {
    renderWithProviders(<KasLoopExhaustedControls row={kasLoopExhaustedRow()} />)
    const count = screen.getByTestId('kas-grant-count')
    expect(count).toHaveValue(1)
    fireEvent.change(count, { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: /grant more passes/i }))
    await waitFor(() => expect(stepAction).toHaveBeenCalledWith('wf-loop', 'loop', 'grant', { path: 'loop', iterations: 2 }))
  })

  it('End the loop and continue completes the loop node', async () => {
    renderWithProviders(<KasLoopExhaustedControls row={kasLoopExhaustedRow()} />)
    fireEvent.click(screen.getByRole('button', { name: /end the loop and continue/i }))
    await waitFor(() => expect(stepAction).toHaveBeenCalledWith('wf-loop', 'loop', 'complete', { path: 'loop', status: 'completed' }))
  })

  it('an invalid count disables Grant; no node.actions means no strip at all', () => {
    const { unmount } = renderWithProviders(<KasLoopExhaustedControls row={kasLoopExhaustedRow()} />)
    fireEvent.change(screen.getByTestId('kas-grant-count'), { target: { value: '0' } })
    expect(screen.getByRole('button', { name: /grant more passes/i })).toBeDisabled()
    unmount()
    const bare = kasLoopExhaustedRow()
    bare.nodes = bare.nodes.map(n => (n.id === 'loop' ? { ...n, actions: [] } : n))
    renderWithProviders(<KasLoopExhaustedControls row={bare} />)
    expect(screen.queryByTestId('kas-loop-exhausted')).toBeNull()
    expect(parseGrantCount('3')).toBe(3)
    expect(parseGrantCount('1.5')).toBeNull()
    expect(parseGrantCount('1001')).toBeNull()
  })
})

describe('2. mark done / failed and stop one step', () => {
  it('Mark done / Mark failed follow node.actions and send complete', async () => {
    const row = kasAwaitingRow()
    const review = findNode(row.nodes, 'review')!
    renderWithProviders(<KasStepDecisionButtons row={row} node={review} />)
    fireEvent.click(screen.getByRole('button', { name: /mark failed/i }))
    await waitFor(() => expect(stepAction).toHaveBeenCalledWith('wf-await', 'review', 'complete', { path: 'loop/review', status: 'failed' }))
    fireEvent.click(screen.getByRole('button', { name: /mark done/i }))
    await waitFor(() => expect(stepAction).toHaveBeenCalledWith('wf-await', 'review', 'complete', { path: 'loop/review', status: 'completed' }))
  })

  it('only the actions listed are drawn', () => {
    const row = kasAwaitingRow()
    const review = { ...findNode(row.nodes, 'review')!, actions: ['reply' as const, 'complete' as const] }
    renderWithProviders(<KasStepDecisionButtons row={row} node={review} />)
    expect(screen.getByRole('button', { name: /mark done/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /mark failed/i })).toBeNull()
  })

  it('Stop on a running step is confirmed, then cancels that step by path', async () => {
    renderWithProviders(<KasStepsTree nodes={kasIncludeRow().nodes} selectedPath={null} onSelect={() => {}} row={kasIncludeRow()} />)
    // The included workflow is a collapsed row; expand it to reach its steps.
    fireEvent.click(screen.getByTestId('kas-include-group'))
    const stops = screen.getAllByRole('button', { name: /^stop test$/i })
    expect(stops).toHaveLength(1)
    fireEvent.click(stops[0])
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Stop this step?')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: /^stop step$/i }))
    await waitFor(() => expect(stepAction).toHaveBeenCalledWith('wf-include', 'review/test', 'cancel', { path: 'review/test' }))
  })
})

describe('3. retry and run again', () => {
  it('Retry failed steps retries the whole run', async () => {
    renderWithProviders(<KasRunRecoveryButtons row={kasFailedRow()} />)
    fireEvent.click(screen.getByRole('button', { name: /retry failed steps/i }))
    await waitFor(() => expect(retryRun).toHaveBeenCalledWith('wf-failed', undefined))
  })

  it('Run again asks first, then restarts', async () => {
    renderWithProviders(<KasRunRecoveryButtons row={kasFailedRow()} />)
    fireEvent.click(screen.getByRole('button', { name: /run again/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /run again/i }))
    await waitFor(() => expect(restartRun).toHaveBeenCalledWith('wf-failed'))
  })

  it('per-step Retry sits on the failed step and targets it', async () => {
    const row = kasFailedRow()
    renderWithProviders(<KasStepsTree nodes={row.nodes} selectedPath={null} onSelect={() => {}} row={row} />)
    fireEvent.click(screen.getByRole('button', { name: /^retry review$/i }))
    await waitFor(() => expect(retryRun).toHaveBeenCalledWith('wf-failed', { nodeId: 'review', path: 'loop/review' }))
  })

  it('no run.actions, no buttons', () => {
    renderWithProviders(<KasRunRecoveryButtons row={kasFailedRow({ actions: ['delete'] })} />)
    expect(screen.queryByRole('button', { name: /retry failed steps/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /run again/i })).toBeNull()
  })
})

describe('4. several workflows in one chat', () => {
  const runs = () => [kasAwaitingRow(), kasIncludeRow(), kasRow({ workflowId: 'wf-3', name: 'Docs pass' })]

  it('combined: one collapsed card "3 workflows · 1 needs you" that expands on click', () => {
    renderWithProviders(<KasChatDockView runs={runs()} slotKey="chat-abc" mode="combined" />)
    const dock = screen.getByTestId('kas-dock')
    expect(dock).toHaveAttribute('data-mode', 'combined')
    expect(dock).toHaveTextContent('3 workflows · 1 needs you')
    expect(screen.queryByTestId('kas-dock-rows')).toBeNull()
    fireEvent.click(screen.getByTestId('kas-dock-toggle'))
    expect(screen.getAllByTestId('kas-dock-row')).toHaveLength(3)
  })

  it('stack: collapsed by default with "+2 more", expands to every card', () => {
    renderWithProviders(<KasChatDockView runs={runs()} slotKey="chat-abc" mode="stack" />)
    const toggle = screen.getByTestId('kas-dock-toggle')
    expect(toggle).toHaveTextContent('+2 more')
    fireEvent.click(toggle)
    expect(screen.getByTestId('kas-dock')).toHaveAttribute('data-expanded', 'true')
  })

  it('one live run draws no dock; the headline pluralises', () => {
    renderWithProviders(<KasChatDockView runs={[kasRow()]} slotKey="chat-abc" mode="combined" />)
    expect(screen.queryByTestId('kas-dock')).toBeNull()
    expect(dockHeadline(1, 0)).toBe('1 workflow')
    expect(dockHeadline(2, 2)).toBe('2 workflows · 2 need you')
    expect(dockSummary(runs()).needsYou).toBe(1)
  })

  it('the Settings switch is device-local', () => {
    renderWithProviders(<KasDockModeSetting />)
    expect(readKasDockMode()).toBe('stack')
    fireEvent.click(screen.getByRole('button', { name: 'One card' }))
    expect(localStorage.getItem(KAS_DOCK_MODE_KEY)).toBe('combined')
    expect(readKasDockMode()).toBe('combined')
  })
})

describe('5. include groups', () => {
  it('labels the group with the recipe, its path on hover, members without the prefix', () => {
    const row = kasIncludeRow()
    renderWithProviders(<KasStepsTree nodes={row.nodes} selectedPath={null} onSelect={() => {}} row={row} />)
    // One collapsed row: the group's own name + "included workflow · N steps",
    // the recipe and its file on hover. It opens to its members (prefix-stripped).
    const group = screen.getByTestId('kas-include-group')
    expect(group).toHaveTextContent('review')
    expect(group).toHaveTextContent('included workflow · 2 steps')
    expect(group).toHaveAttribute('title', '.kiro/workflows/code-review.yaml')
    expect(screen.queryByText('Lint')).toBeNull()
    fireEvent.click(group)
    expect(screen.getByText('Lint')).toBeInTheDocument()
    expect(screen.getByText('Test')).toBeInTheDocument()
    expect(screen.queryByText('review/Lint')).toBeNull()
    expect(includeMemberLabel('review/Lint', 'review')).toBe('Lint')
    expect(includeMemberLabel('other/Lint', 'review')).toBe('other/Lint')
  })

  it('the live line (card and dock) names an included step without its prefix', () => {
    expect(liveStatusLine(kasIncludeRow())).toMatchObject({ kind: 'active', text: 'Test' })
    expect(stepDisplayLabel(kasIncludeRow().nodes, 'review/test', 'review/Test')).toBe('Test')
    expect(stepDisplayLabel(kasRow().nodes, 'review', 'Review')).toBe('Review')
  })
})

describe('6. plan approval tree', () => {
  it('draws plan.nodes as a step tree above the plan text', () => {
    renderWithProviders(<KasApprovalCard approval={kasPlanApproval()} pending={false} onAnswer={() => {}} />)
    const tree = screen.getByTestId('kas-steps-tree')
    const text = screen.getByText(/plan: Plan/)
    expect(tree.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(tree).getByText('Fix loop')).toBeInTheDocument()
    // A plan preview has run no passes, so the loop names its budget.
    expect(within(tree).getByText('Iterations: up to 3')).toBeInTheDocument()
    // The included workflow is one collapsible row (recipe on hover); open it for its member.
    const include = within(tree).getByTestId('kas-include-group')
    expect(include).toHaveTextContent('included workflow')
    fireEvent.click(include)
    expect(within(tree).getByText('Write')).toBeInTheDocument()
    expect(planTreeNodes({ nodes: 'nope' })).toEqual([])
  })
})

describe('7. pause reason line', () => {
  const line = (reason: string, extra: object = {}) =>
    pauseLine({ ...kasAwaitingRow(), pause: { reason, nodeId: 'review', path: 'loop/review', ...extra } })?.text

  it('names each reason', () => {
    expect(line('transient_error')).toBe('Paused on a temporary error. Resume to retry.')
    expect(line('retries_exhausted')).toBe('Review failed and used all its retries.')
    expect(line('awaiting_input')).toBe('Review is waiting for your reply.')
    expect(line('plan_approval')).toBe('Waiting for you to approve the plan.')
    expect(pauseLine(kasLoopExhaustedRow())?.text).toBe('Fix loop reached its iteration limit (3).')
    expect(pauseLine(kasFailedRow({ pause: { reason: 'transient_error' } }))).toBeNull()
  })

  it('shows KAS detail muted after the line', () => {
    renderWithProviders(
      <KasPauseReasonLine row={{ ...kasAwaitingRow(), pause: { reason: 'transient_error', message: 'HTTP 503 from model' } }} />,
    )
    const p = screen.getByTestId('kas-pause-reason')
    expect(p).toHaveAttribute('data-reason', 'transient_error')
    expect(p).toHaveTextContent('Paused on a temporary error. Resume to retry. · HTTP 503 from model')
  })
})

describe('8. question card', () => {
  it('sends one choice per question, several for multi-select', async () => {
    renderWithProviders(<KasQuestionCard question={kasQuestion()} stepLabel="Review" />)
    expect(screen.getByText('Question from Review')).toBeInTheDocument()
    const send = screen.getByRole('button', { name: /^send$/i })
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByLabelText(/Everything flagged/))
    fireEvent.click(screen.getByLabelText('Lint'))
    fireEvent.click(screen.getByLabelText('Typecheck'))
    expect(send).toBeEnabled()
    fireEvent.click(send)
    await waitFor(() =>
      expect(answerQuestion).toHaveBeenCalledWith('q-1', { answers: { scope: 'Everything flagged', checks: ['Lint', 'Typecheck'] } }),
    )
  })

  it('free text replaces the picked option; Dismiss sends dismiss', async () => {
    renderWithProviders(<KasQuestionCard question={kasQuestion({ questions: [kasQuestion().questions[0]] })} />)
    fireEvent.click(screen.getByLabelText(/Only the failing tests/))
    fireEvent.change(screen.getByPlaceholderText('Other'), { target: { value: 'Just the flaky one' } })
    fireEvent.click(screen.getByRole('button', { name: /^send$/i }))
    await waitFor(() => expect(answerQuestion).toHaveBeenCalledWith('q-1', { answers: { scope: 'Just the flaky one' } }))
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(answerQuestion).toHaveBeenCalledWith('q-1', { dismiss: true }))
  })

  it('appears in the run detail for its own run only', () => {
    renderWithProviders(
      <KasRunDetail
        row={kasRow()}
        approvals={[]}
        questions={[kasQuestion(), kasQuestion({ key: 'q-2', workflowId: 'wf-other' })]}
        slotKey="chat-abc"
        selectedNodeId={undefined}
        onSelectStep={() => {}}
        onBack={() => {}}
      />,
    )
    expect(screen.getAllByTestId('kas-question-card')).toHaveLength(1)
  })
})

describe('10. waiting for your reply', () => {
  it('a step that awaits a reply says so in the tree, not "In progress"', () => {
    const row = kasAwaitingRow()
    renderWithProviders(<KasStepsTree nodes={row.nodes} selectedPath={null} onSelect={() => {}} row={row} />)
    expect(screen.getByTestId('kas-step-waiting')).toHaveTextContent('Waiting for your reply')
  })

  it('the run detail puts Mark done / Mark failed by the reply box of the paused step', () => {
    renderWithProviders(
      <KasRunDetail row={kasAwaitingRow()} approvals={[]} slotKey="chat-abc" selectedNodeId="review" onSelectStep={() => {}} onBack={() => {}} />,
    )
    expect(screen.getByTestId('kas-step-decision')).toBeInTheDocument()
    expect(screen.getByTestId('kas-pause-reason')).toHaveTextContent('Review is waiting for your reply.')
  })
})
