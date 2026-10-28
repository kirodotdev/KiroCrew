import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../../../api/client'
import { ApiError } from '../../../api/apiError'
import { createTestStore, renderWithProviders } from '../../../test/helpers'
import { kasApproval, kasControls, kasLoopRow, kasRow, kasTranscript } from '../../../test/kasFixtures'
import { KasRunDetail, KasStepsTree } from './KasRunDetail'
import { KasStepChat } from './KasStepChat'
import { foldReplies } from './kasModel'

beforeEach(() => {
  vi.spyOn(api, 'kasRunControl').mockResolvedValue({ workflowId: 'wf-1', row: null })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const renderDetail = (row = kasRow(), extra: Partial<Parameters<typeof KasRunDetail>[0]> = {}, store = createTestStore()) => {
  const onSelectStep = vi.fn()
  const onBack = vi.fn()
  const utils = renderWithProviders(
    <KasRunDetail row={row} approvals={[]} slotKey="chat-abc" selectedNodeId={undefined} onSelectStep={onSelectStep} onBack={onBack} {...extra} />,
    { store },
  )
  return { ...utils, onSelectStep, onBack }
}

describe('KasStepsTree', () => {
  const nodes = kasRow({
    nodes: [
      ...kasRow().nodes,
      { ...kasRow().nodes[0], id: 'gone', path: 'gone', label: 'Gone', status: 'skipped', sessionId: null, agent: null },
      { ...kasRow().nodes[0], id: 'bad', path: 'bad', label: 'Bad', status: 'failed', sessionId: 's-bad', agent: null },
      { ...kasRow().nodes[0], id: 'halt', path: 'halt', label: 'Halt', status: 'aborted', sessionId: null, agent: null },
    ],
  }).nodes

  it('draws one glyph per status and the agent per step', () => {
    renderWithProviders(<KasStepsTree nodes={nodes} selectedPath={null} onSelect={() => {}} />)
    const glyph = (label: string) => screen.getByText(label).closest('[data-testid="kas-tree-step"]')!.querySelector('[data-glyph]')!.getAttribute('data-glyph')
    expect(glyph('Plan')).toBe('completed')
    expect(glyph('Review')).toBe('running')
    expect(glyph('Fix')).toBe('pending')
    expect(glyph('Gone')).toBe('skipped')
    expect(glyph('Bad')).toBe('failed')
    expect(glyph('Halt')).toBe('stopped')
    expect(screen.getAllByTestId('kas-step-meta').map(t => t.textContent)).toEqual(['planner', 'reviewer', 'fixer'])
    // The loop row shows its passes against the budget, not "Iteration n/m".
    expect(screen.getByTestId('kas-step-group')).toHaveTextContent('1/3')
    expect(within(screen.getByTestId('kas-step-group')).getByTestId('kas-loop-passes')).toBeInTheDocument()
  })

  it('a group collapses and expands', () => {
    renderWithProviders(<KasStepsTree nodes={nodes} selectedPath={null} onSelect={() => {}} />)
    const group = screen.getByTestId('kas-step-group')
    expect(group).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(group)
    expect(group).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Review')).toBeNull()
    fireEvent.click(group)
    expect(screen.getByText('Review')).toBeInTheDocument()
  })

  it('only a step with a session is selectable; the selected one is aria-current', () => {
    const onSelect = vi.fn()
    renderWithProviders(<KasStepsTree nodes={nodes} selectedPath="plan" onSelect={onSelect} />)
    const plan = screen.getByText('Plan').closest('[data-testid="kas-tree-step"]')!
    expect(plan).toHaveAttribute('aria-current', 'true')
    fireEvent.click(screen.getByText('Review'))
    expect(onSelect).toHaveBeenCalledWith('review', 'loop/review')
    // Fix has no session: plain text, not a button.
    expect(screen.getByText('Fix').closest('button')).toBeNull()
  })

  it('renders a loop body once (latest iteration) with a pager that selects another instance', () => {
    const onSelect = vi.fn()
    renderWithProviders(<KasStepsTree nodes={kasLoopRow().nodes} selectedPath="loop/iter-1/review" onSelect={onSelect} />)
    // The loop row shows realised passes against the budget.
    expect(screen.getByTestId('kas-loop-passes')).toHaveTextContent('2/3')
    // The body renders ONCE, from the latest iteration: Review + Fix, no per-iteration groups.
    expect(screen.queryByTestId('kas-iteration-group')).toBeNull()
    expect(screen.getAllByText('Review')).toHaveLength(1)
    const selected = screen.getByText('Review').closest('[data-testid="kas-tree-step"]')!
    expect(selected).toHaveAttribute('aria-current', 'true')
    // The selected looped step carries an iteration pager at its latest instance.
    const pager = screen.getByTestId('kas-iteration-pager')
    expect(pager).toHaveTextContent('2/2')
    // Paging back switches the instance shown below to iteration 1's.
    fireEvent.click(within(pager).getByRole('button', { name: /previous run/i }))
    expect(onSelect).toHaveBeenCalledWith('review', 'loop/iter-0/review')
  })
})

describe('KasRunDetail', () => {
  it('header, controls, and the active step chosen by default', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    const { onBack } = renderDetail()
    const detail = screen.getByTestId('kas-run-detail')
    expect(within(detail).getByRole('heading', { name: 'Review and fix' })).toBeInTheDocument()
    expect(screen.getByTestId('kas-run-progress')).toHaveTextContent('1/3 steps')
    expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /open chat/i })).toBeNull()
    expect(screen.getByText('Review').closest('[data-testid="kas-tree-step"]')).toHaveAttribute('aria-current', 'true')
    await waitFor(() => expect(api.kasStepTranscript).toHaveBeenCalledWith('wf-1', 'review', 'loop/review'))
    fireEvent.click(screen.getByTestId('kas-detail-back'))
    expect(onBack).toHaveBeenCalled()
  })

  it('a blocked run shows its blocking line and approvals, and opens on the blocking step', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript({ nodeId: 'fix' }))
    renderDetail(
      kasRow({
        activeSteps: [],
        blocking: { nodeId: 'fix', label: 'Fix', kind: 'question', sessionId: 's-fix', toolCallId: null },
        controls: kasControls({ primary: 'open', needsAttention: true }),
      }),
      { approvals: [kasApproval()] },
    )
    expect(screen.getByTestId('kas-detail-blocking')).toHaveTextContent('Fix — Waiting for an answer')
    expect(screen.getByTestId('kas-approval')).toBeInTheDocument()
    await waitFor(() => expect(api.kasStepTranscript).toHaveBeenCalledWith('wf-1', 'fix', 'loop/fix'))
  })

  it('a finished run opens on its last finished step and offers Delete', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript({ nodeId: 'plan', status: 'idle' }))
    renderDetail(
      kasRow({
        status: 'completed',
        activeSteps: [],
        completedSteps: 3,
        controls: kasControls({ display: 'completed', primary: null, canStop: false, canDelete: true }),
      }),
    )
    // A finished run still shows its leaf-step tally (the reference's 49/49).
    expect(screen.getByTestId('kas-run-progress')).toHaveTextContent('3/3 steps')
    expect(screen.getByRole('button', { name: /^delete$/i })).toBeInTheDocument()
    await waitFor(() => expect(api.kasStepTranscript).toHaveBeenCalledWith('wf-1', 'plan', 'plan'))
  })

  it('the reader’s choice wins over the default step', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript({ nodeId: 'plan' }))
    renderDetail(kasRow(), { selectedNodeId: 'plan' })
    await waitFor(() => expect(api.kasStepTranscript).toHaveBeenCalledWith('wf-1', 'plan', 'plan'))
    expect(screen.getByText('Plan').closest('[data-testid="kas-tree-step"]')).toHaveAttribute('aria-current', 'true')
  })

  it('names the parent chat in the breadcrumb, from the store', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    const store = createTestStore()
    const base = store.getState().dashboard
    const seeded = createTestStore({ dashboard: { ...base, slots: [{ key: 'chat-abc', title: 'Workflow launch' } as never] } })
    renderDetail(kasRow(), {}, seeded)
    expect(await screen.findByTestId('kas-step-breadcrumb')).toHaveTextContent('From Workflow launch › reviewer')
  })

  it('lays the steps tree as a full-width table above the step chat', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    renderDetail()
    const tree = await screen.findByTestId('kas-steps-tree')
    const chat = screen.getByTestId('kas-step-chat')
    // Tree on top, chat below — one column, no side-by-side layout toggle.
    expect(tree.compareDocumentPosition(chat) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('kas-run-detail')).not.toHaveAttribute('data-layout')
  })

  it('names the parent chat on the right and links it', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    const store = createTestStore()
    const base = store.getState().dashboard
    const seeded = createTestStore({ dashboard: { ...base, slots: [{ key: 'chat-abc', title: 'Workflow launch' } as never] } })
    renderDetail(kasRow(), {}, seeded)
    expect(screen.getByTestId('kas-created-by')).toHaveTextContent('Created by session Workflow launch')
    expect(screen.getByTestId('kas-created-by-link')).toHaveTextContent('Workflow launch')
  })
})

describe('KasStepChat', () => {
  it('renders the prompt, a per-reply chip that expands to its narration and tool rows, the reply and footer', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript({ status: 'idle' }))
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" stepRunning={false} chatTitle={null} agent="reviewer" />)
    expect(await screen.findByTestId('kas-step-user')).toHaveTextContent('Review the diff for bugs.')
    expect(screen.getByTestId('kas-step-breadcrumb')).toHaveTextContent('From this chat › reviewer')
    // The reply is always visible; its work folds behind a right-aligned chip
    // (closed by default, "Collapse all steps" on).
    const reply = screen.getByTestId('kas-step-assistant')
    expect(reply).toHaveTextContent('Found one issue')
    expect(reply.querySelector('strong')).toHaveTextContent('one')
    const chip = screen.getByTestId('kas-step-chip')
    expect(chip).toHaveTextContent('Thought (1) + Tools (1)')
    expect(chip).toHaveAttribute('data-open', 'false')
    expect(screen.queryByTestId('kas-step-tool')).toBeNull()
    expect(screen.queryByTestId('kas-step-reasoning')).toBeNull()
    // Open the chip: the short thinking reads as inline narration, the tool row by its command.
    fireEvent.click(within(chip).getByRole('button'))
    const reasoning = screen.getByTestId('kas-step-reasoning')
    expect(reasoning.tagName).not.toBe('DETAILS')
    expect(reasoning).toHaveTextContent('Let me look at the changed files first.')
    const tool = screen.getByTestId('kas-step-tool')
    expect(tool).toHaveTextContent('git diff --stat')
    expect(tool).not.toHaveTextContent('Run Command')
    const toggle = within(tool).getByRole('button')
    expect(toggle).toHaveAttribute('title', 'Run Command')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(within(tool).getByText(/1 file changed/)).toBeInTheDocument()
    expect(screen.getByTestId('kas-step-footer')).toHaveTextContent('0.23 credits · 1m 5s · Done')
  })

  it('folds the transcript per reply: prompt / chip+reply / trailing work / parent card', () => {
    const user = { kind: 'user' as const, id: 'u', ts: null, text: 'go' }
    const think = { kind: 'assistant' as const, id: 'r', ts: null, text: 'hm', reasoning: true }
    const say = (id: string) => ({ kind: 'assistant' as const, id, ts: '2026-10-16T10:00:00Z', text: id, reasoning: false })
    const tool = (id: string) => ({
      kind: 'tool' as const, id, ts: null, toolCallId: id, name: 'x', title: null, toolKind: 'execute', status: 'completed', input: id, output: null,
    })
    const parent = {
      kind: 'tool' as const, id: 'p', ts: null, toolCallId: 'p', name: 'send_message', title: null, toolKind: null, status: 'completed',
      input: '{"message":"done","severity":"success"}', output: null,
    }
    const blocks = foldReplies([user, think, tool('t1'), say('mid'), tool('t2'), parent, say('end')])
    expect(blocks.map(b => b.kind)).toEqual(['prompt', 'reply', 'work', 'parent', 'reply'])
    const first = blocks[1]
    expect(first.kind === 'reply' && first.chip ? [first.chip.thoughts, first.chip.tools] : null).toEqual([1, 1])
    const trailing = blocks[2]
    expect(trailing.kind === 'work' ? [trailing.chip.thoughts, trailing.chip.tools] : null).toEqual([0, 1])
    const last = blocks[4]
    // Nothing happened between the parent card and the final reply, so no chip.
    expect(last.kind === 'reply' ? last.chip : 'x').toBeNull()
  })

  it('a long thinking block folds to DETAILS inside its chip', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(
      kasTranscript({ status: 'idle', entries: [{ kind: 'assistant', id: 'r', ts: null, text: 'x'.repeat(600), reasoning: true }] }),
    )
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" stepRunning={false} chatTitle="x" agent={null} />)
    const chip = await screen.findByTestId('kas-step-chip')
    expect(chip).toHaveTextContent('Thought (1)')
    fireEvent.click(within(chip).getByRole('button'))
    const reasoning = screen.getByTestId('kas-step-reasoning')
    expect(reasoning.tagName).toBe('DETAILS')
    expect(reasoning).not.toHaveAttribute('open')
  })

  it('collapses a long prompt behind Show more', async () => {
    const long = Array.from({ length: 12 }, (_, i) => `line ${i}`).join('\n')
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(
      kasTranscript({ status: 'idle', entries: [{ kind: 'user', id: 'u', ts: null, text: long }] }),
    )
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" stepRunning={false} chatTitle="x" agent={null} />)
    const more = await screen.findByRole('button', { name: /show more/i })
    const body = () => screen.getByTestId('kas-step-user').firstElementChild as HTMLElement
    expect(body()).toHaveClass('overflow-hidden')
    fireEvent.click(more)
    expect(screen.getByRole('button', { name: /show less/i })).toBeInTheDocument()
    expect(body()).not.toHaveClass('overflow-hidden')
  })

  it('renders the step prompt as Markdown, not raw text', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(
      kasTranscript({
        status: 'idle',
        entries: [{ kind: 'user', id: 'u', ts: null, text: '## Investigation brief\n\n1. Stack\n2. Pages' }],
      }),
    )
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" stepRunning={false} chatTitle="x" agent={null} />)
    expect(await screen.findByRole('heading', { name: 'Investigation brief' })).toBeInTheDocument()
    expect(screen.getByTestId('kas-step-user')).not.toHaveTextContent('##')
    expect(screen.getByTestId('kas-step-user').querySelectorAll('li')).toHaveLength(2)
  })

  it('says a step has not started on 404 step_unknown', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockRejectedValue(new ApiError(404, 'no session', JSON.stringify({ code: 'step_unknown' })))
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="fix" stepRunning={false} chatTitle={null} agent="fixer" />)
    expect(await screen.findByTestId('kas-step-not-started')).toHaveTextContent('This step has not started.')
  })

  it('polls every ~2s while the step runs', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const read = vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript())
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" stepRunning chatTitle={null} agent={null} />)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('kas-step-footer')).toHaveTextContent('In progress')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100)
    })
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
  })

  it('reads a finished step once', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const read = vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript({ status: 'idle' }))
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="plan" stepRunning={false} chatTitle={null} agent={null} />)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6500)
    })
    expect(read).toHaveBeenCalledTimes(1)
  })
})

describe('KasStepChat — reply composer', () => {
  beforeEach(() => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(kasTranscript({ status: 'idle' }))
  })

  it('is present but disabled, with a one-line reason, until the step awaits a reply', async () => {
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" path="loop/iter-1/review" stepRunning={false} chatTitle={null} agent="reviewer" />)
    const form = await screen.findByTestId('kas-step-reply')
    expect(form).toHaveAttribute('data-can-reply', 'false')
    expect(screen.getByLabelText('Reply to this step')).toBeDisabled()
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
    expect(screen.getByTestId('kas-step-reply-hint')).toBeInTheDocument()
  })

  it('sends the reply with the step path, clears on success, and refetches', async () => {
    const send = vi.spyOn(api, 'kasReplyToStep').mockResolvedValue({ sent: true })
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" path="loop/iter-1/review" stepRunning={false} canReply chatTitle={null} agent="reviewer" />)
    const box = (await screen.findByLabelText('Reply to this step')) as HTMLTextAreaElement
    const button = screen.getByRole('button', { name: /send/i })
    expect(button).toBeDisabled()
    fireEvent.change(box, { target: { value: '  retry the failing test  ' } })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    await waitFor(() => expect(send).toHaveBeenCalledWith('wf-1', 'review', 'retry the failing test', 'loop/iter-1/review'))
    await waitFor(() => expect(box.value).toBe(''))
  })

  it('pins the composer to the pane bottom; Mark done and Send sit on the right, Send last', async () => {
    renderWithProviders(
      <KasStepChat
        workflowId="wf-1"
        nodeId="plan"
        path={null}
        stepRunning={false}
        canReply
        chatTitle={null}
        agent="planner"
        stepActions={<button type="button">Mark done</button>}
      />,
    )
    const form = await screen.findByTestId('kas-step-reply')
    expect(form.className).toMatch(/\bsticky\b/)
    expect(form.className).toMatch(/\bbottom-0\b/)
    const row = screen.getByTestId('kas-step-reply-actions')
    const buttons = Array.from(row.querySelectorAll('button')).map(b => b.textContent?.trim())
    expect(buttons).toEqual(['Mark done', 'Send'])
  })

  it('Ctrl+Enter sends', async () => {
    const send = vi.spyOn(api, 'kasReplyToStep').mockResolvedValue({ sent: true })
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" path={null} stepRunning={false} canReply chatTitle={null} agent="reviewer" />)
    const box = await screen.findByLabelText('Reply to this step')
    fireEvent.change(box, { target: { value: 'go on' } })
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true })
    await waitFor(() => expect(send).toHaveBeenCalledWith('wf-1', 'review', 'go on', null))
  })

  it('shows the server error inline and keeps the draft', async () => {
    vi.spyOn(api, 'kasReplyToStep').mockRejectedValue(
      new ApiError(409, 'This step is not waiting for a reply.', JSON.stringify({ code: 'kas_workflow_error' })),
    )
    renderWithProviders(<KasStepChat workflowId="wf-1" nodeId="review" path={null} stepRunning={false} canReply chatTitle={null} agent="reviewer" />)
    const box = (await screen.findByLabelText('Reply to this step')) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(await screen.findByTestId('kas-step-reply-error')).toHaveTextContent('This step is not waiting for a reply.')
    expect(box.value).toBe('hello')
  })
})
