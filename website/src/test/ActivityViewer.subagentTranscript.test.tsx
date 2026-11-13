/**
 * The Subagents-panel card's live mini-transcript and status label. A card
 * whose agent works through tools without streaming answer text used to read
 * "Starting…" over "Waiting for output…" for its whole run. These pin:
 *  - any evidence of work (a tool row, reasoning) flips the label off "Starting…";
 *  - a card with transcript rows renders them -- folded reasoning, tool rows
 *    that open on their input and output, answer text -- in place of the
 *    single Output box and the plain "Tool calls" list;
 *  - a card without rows keeps the previous rendering.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

vi.mock('../api/client', () => ({
  api: {
    spawnStatus: vi.fn().mockResolvedValue({ result: '' }),
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnRetry: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
    approveChatSlot: vi.fn().mockResolvedValue({}),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    createArtifact: vi.fn().mockResolvedValue({ slug: 'new-slug', version: 1 }),
    setArtifactPinned: vi.fn().mockResolvedValue({}),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
  },
}))

vi.mock('../components/MarkdownPanel', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<{ requestClose: () => void }, { filePath: string; content: string; onClose: () => void }>(
      ({ filePath, content, onClose }, ref) => {
        useImperativeHandle(ref, () => ({ requestClose: onClose }), [onClose])
        return <div data-testid="md-panel">{filePath}::{content}</div>
      },
    ),
  }
})

import ActivityViewer from '../pages/chat/ActivityViewer'
import { createTestStore } from './helpers'
import type { SubagentActivity, ToolActivity } from '../types'

const SLOT = 'test-slot'

function renderPanel(ui: React.ReactElement) {
  const store = createTestStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
  }
  return render(ui, { wrapper: Wrapper })
}

const baseProps = {
  subagents: {} as Record<string, SubagentActivity>,
  toolLog: [] as ToolActivity[],
  open: true,
  onToggle: vi.fn(),
  slot: SLOT,
}

const mkAgent = (id: string, over: Partial<SubagentActivity> = {}): SubagentActivity => ({
  id,
  task: `task for ${id}`,
  agent: `ag-${id}`,
  status: 'running',
  streaming: '',
  lastTool: '',
  startedAt: 1_700_000_000_000,
  elapsed: 4,
  ...over,
})

const panel = (a: SubagentActivity) => (
  <ActivityViewer {...baseProps} view="subagents" subagents={{ [a.id]: a }} />
)

describe('ActivityViewer — subagent card status label', () => {
  it('says Starting… only while nothing has been heard from the agent', () => {
    renderPanel(panel(mkAgent('s1')))
    expect(screen.getByText('Starting…')).toBeInTheDocument()
    expect(screen.getByText('Waiting for output…')).toBeInTheDocument()
  })

  it('reads Running once the agent has reasoned, with no answer text yet', () => {
    renderPanel(panel(mkAgent('s1', { transcript: [{ kind: 'thinking', seq: 0, text: 'weighing options' }] })))
    expect(screen.getByText('Running')).toBeInTheDocument()
    expect(screen.queryByText('Starting…')).not.toBeInTheDocument()
    expect(screen.queryByText('Waiting for output…')).not.toBeInTheDocument()
  })

  it('reads Running for a card that only knows its tool calls', () => {
    renderPanel(panel(mkAgent('s1', { transcript: [{ kind: 'tool', seq: 0, tool: 'Reading a.ts', status: 'completed' }] })))
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('draws no transcript on a card the inventory retired', () => {
    // A retired card claims only that the inventory holds no row for it; rows
    // it kept from before would read as a run that happened.
    renderPanel(panel(mkAgent('s1', {
      status: 'stopped', approval_id: 'ap-1', approvalGone: 'ap-1', approvalRetired: true,
      transcript: [{ kind: 'tool', seq: 0, id: 'tc-1', tool: 'Reading a.ts', output: 'x', status: 'completed' }],
    })))
    expect(screen.getByText('No record')).toBeInTheDocument()
    // Expanded too: the rows the card kept stay off it.
    fireEvent.click(screen.getByText('No record').closest('[aria-expanded]')!)
    expect(screen.queryByTestId('subagent-transcript')).not.toBeInTheDocument()
    expect(screen.queryByText('Reading a.ts')).not.toBeInTheDocument()
    expect(screen.queryByTestId('subagent-output-body')).not.toBeInTheDocument()
  })
})

describe('ActivityViewer — subagent live transcript', () => {
  const live = mkAgent('native:c1', {
    status: 'tool',
    lastTool: 'Running: npm test',
    transcript: [
      { kind: 'thinking', seq: 0, text: 'plan the review' },
      { kind: 'tool', seq: 1, id: 'tc-1', tool: 'Running: git log -1', input: '{"command": "git log -1"}', output: 'commit abc', status: 'completed' },
      { kind: 'text', seq: 2, text: 'The commit looks **fine**.' },
      { kind: 'tool', seq: 3, id: 'tc-2', tool: 'Running: npm test', input: '{"command": "npm test"}' },
    ],
  })

  it('renders the rows in order and hides the Output box', () => {
    renderPanel(panel(live))
    const list = screen.getByTestId('subagent-transcript')
    expect(screen.getByTestId('subagent-transcript-heading')).toHaveTextContent('Live activity')
    const tools = within(list).getAllByTestId('subagent-transcript-tool')
    // kiro-cli's "Running: " status prefix is dropped from every row; the live
    // one is marked by aria-current instead.
    expect(tools.map(t => t.textContent)).toEqual(['git log -1', 'npm test'])
    expect(tools[0]).toHaveAttribute('data-tool-status', 'completed')
    expect(tools[0]).not.toHaveAttribute('aria-current')
    expect(tools[1]).toHaveAttribute('data-tool-status', 'running')
    expect(tools[1]).toHaveAttribute('aria-current', 'step')
    expect(within(list).getByText('fine')).toBeInTheDocument()
    expect(screen.queryByTestId('subagent-output-body')).not.toBeInTheDocument()
  })

  it('says how many calls the coalescer merged away when no row was dropped', () => {
    renderPanel(panel(mkAgent('s1', { status: 'tool', toolCount: 5, transcript: [{ kind: 'tool', seq: 0, tool: 'a' }, { kind: 'tool', seq: 1, tool: 'b' }] })))
    expect(screen.getByTestId('subagent-transcript-unlisted')).toHaveTextContent('3 earlier calls not shown')
    expect(screen.queryByTestId('subagent-transcript-omitted')).not.toBeInTheDocument()
  })

  it('fades the edges rows sit beyond; the solid box around the scroller stays solid', () => {
    renderPanel(panel(live))
    const ol = within(screen.getByTestId('subagent-transcript')).getByRole('list')
    expect(ol).not.toHaveAttribute('data-fade-top')
    expect(ol).not.toHaveAttribute('data-fade-bottom')
    expect(ol).not.toHaveClass('bg-bg')
    expect(ol.parentElement).toHaveClass('bg-bg')
    // Rows above the box: fade the top edge.
    Object.defineProperty(ol, 'scrollHeight', { configurable: true, value: 600 })
    Object.defineProperty(ol, 'clientHeight', { configurable: true, value: 320 })
    ol.scrollTop = 280
    fireEvent.scroll(ol)
    expect(ol).toHaveAttribute('data-fade-top')
    expect(ol).not.toHaveAttribute('data-fade-bottom')
    // Scrolled back up with rows still below: fade the bottom edge only.
    ol.scrollTop = 0
    fireEvent.scroll(ol)
    expect(ol).not.toHaveAttribute('data-fade-top')
    expect(ol).toHaveAttribute('data-fade-bottom')
    expect(ol.style.maskImage).toContain('transparent')
  })

  it('keeps following the newest row past the cap when neither the row count nor the last length moves', () => {
    // At the cap every append drops the oldest row, so the row count stays at
    // 200; two tool rows with no output in a row leave the last length at 0 as
    // well. The follow effect used to key on those two and so never re-ran:
    // the list stopped at the old bottom and the edge fade went stale.
    const capped = (from: number) => Array.from({ length: 200 }, (_, i) => (
      { kind: 'tool', seq: from + i, id: `tc-${from + i}`, tool: `call ${from + i}`, status: 'completed' } as const
    ))
    const { rerender } = renderPanel(panel(mkAgent('native:c1', { status: 'tool', transcript: capped(0) })))
    const ol = within(screen.getByTestId('subagent-transcript')).getByRole('list')
    Object.defineProperty(ol, 'scrollHeight', { configurable: true, value: 4000 })
    Object.defineProperty(ol, 'clientHeight', { configurable: true, value: 320 })
    // The reader has not scrolled, so the list is still following; the old
    // bottom is simply where the last run of the effect left it.
    ol.scrollTop = 3600
    expect(ol).not.toHaveAttribute('data-fade-top')
    rerender(panel(mkAgent('native:c1', { status: 'tool', transcript: capped(1) })))
    expect(ol.scrollTop).toBe(4000)
    expect(ol).toHaveAttribute('data-fade-top')
    expect(ol).not.toHaveAttribute('data-fade-bottom')
  })

  it("names each tool row's toggle by its call, so rows can be told apart by assistive tech", () => {
    // A shared "Show this call's input and output" label had named every row
    // alike; the command is the name, and aria-expanded carries the state.
    renderPanel(panel(live))
    const [first] = screen.getAllByTestId('subagent-transcript-tool')
    const toggle = within(first).getByRole('button', { name: 'git log -1' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).not.toHaveAttribute('aria-label')
    fireEvent.click(toggle)
    expect(within(first).getByRole('button', { name: 'git log -1' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('opens a tool row on its input and output', () => {
    renderPanel(panel(live))
    const [first] = screen.getAllByTestId('subagent-transcript-tool')
    expect(within(first).queryByText('commit abc')).not.toBeInTheDocument()
    fireEvent.click(within(first).getByRole('button', { name: 'git log -1' }))
    expect(within(first).getByText('Input')).toBeInTheDocument()
    expect(within(first).getByText('Output')).toBeInTheDocument()
    expect(within(first).getByText('commit abc')).toBeInTheDocument()
    // The input block carries the JSON payload, distinct from the row's title.
    expect(within(first).getByText(/"command"/)).toBeInTheDocument()
    fireEvent.click(within(first).getByRole('button', { name: 'git log -1' }))
    expect(within(first).queryByText('commit abc')).not.toBeInTheDocument()
  })

  it('renders a failed call\'s output as the error surface, with the hand-off on', () => {
    // The output of a call that failed is its diagnostics -- the outcome of
    // something that failed -- so it is an error, and errors render through
    // ErrorNotice: the one surface that offers the agent the hand-off.
    renderPanel(panel(mkAgent('native:c1', {
      status: 'tool',
      transcript: [
        { kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: npm test', input: '{"command": "npm test"}', output: 'FAIL src/a.test.ts\n  exit code 1', status: 'failed' },
        { kind: 'tool', seq: 1, id: 'tc-2', tool: 'Reading a.ts', output: 'const a = 1', status: 'completed' },
      ],
    })))
    const [failed, ok] = screen.getAllByTestId('subagent-transcript-tool')
    expect(failed).toHaveAttribute('data-tool-status', 'failed')
    fireEvent.click(within(failed).getByRole('button', { name: 'npm test' }))
    const notice = within(failed).getByTestId('subagent-transcript-tool-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Tool call failed')
    expect(notice).toHaveTextContent('exit code 1')
    // A scoped label: the card can show several notices at once.
    expect(within(notice).getByText('Ask the agent about this failed call')).toBeInTheDocument()
    // The failed row has no plain "Output" box beside the notice.
    expect(within(failed).queryByText('Output')).not.toBeInTheDocument()
    // A completed call's output stays plain output: nothing failed.
    fireEvent.click(within(ok).getByRole('button', { name: 'Reading a.ts' }))
    expect(within(ok).getByText('Output')).toBeInTheDocument()
    expect(within(ok).getByText('const a = 1')).toBeInTheDocument()
    expect(within(ok).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps a call whose newest frame says in_progress live, not failed', () => {
    // kiro-cli's content update for a running call carries `status:
    // in_progress` with its partial output. That is not an outcome: the row
    // stays live (spinner, aria-current) and its output is plain Output, with
    // no error surface -- only a terminal failed/cancelled/refused is a failure.
    renderPanel(panel(mkAgent('native:c1', {
      status: 'tool',
      transcript: [
        { kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: make', output: 'collecting…', status: 'in_progress' },
        { kind: 'tool', seq: 1, id: 'tc-2', tool: 'Reading a.ts', status: 'pending' },
      ],
    })))
    const [building, pending] = screen.getAllByTestId('subagent-transcript-tool')
    expect(within(building).queryByLabelText('Error')).not.toBeInTheDocument()
    fireEvent.click(within(building).getByRole('button', { name: 'make' }))
    expect(within(building).queryByRole('alert')).not.toBeInTheDocument()
    expect(within(building).getByText('Output')).toBeInTheDocument()
    expect(building).toHaveAttribute('data-tool-status', 'in_progress')
    expect(building).toHaveAttribute('aria-current', 'step')
    expect(within(building).getByLabelText('Running')).toBeInTheDocument()
    expect(within(pending).queryByLabelText('Error')).not.toBeInTheDocument()
    expect(pending).toHaveAttribute('aria-current', 'step')
  })

  it('renders a failure reported by status alone as the error surface, not just a red glyph', () => {
    // kiro-cli's terminal frame can carry a failed status and no output. The
    // failure is still the outcome of something that failed: it opens, on
    // ErrorNotice, with the hand-off -- a row that could not be opened would
    // bury the failure in an icon.
    renderPanel(panel(mkAgent('native:c1', {
      status: 'tool',
      transcript: [{ kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: npm test', status: 'failed' }],
    })))
    const [failed] = screen.getAllByTestId('subagent-transcript-tool')
    expect(failed).toHaveAttribute('data-tool-status', 'failed')
    fireEvent.click(within(failed).getByRole('button', { name: 'npm test' }))
    const notice = within(failed).getByTestId('subagent-transcript-tool-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Tool call failed')
    expect(notice).toHaveTextContent('No output was returned.')
    expect(within(notice).getByText('Ask the agent about this failed call')).toBeInTheDocument()
  })

  it('mutes a failed call while the card runs and once it completes; reddens it when the card ends in failure', () => {
    // A reviewer's failing test is an ordinary non-zero exit on a run that is
    // still going and handling it: red on every such row made a working agent
    // look like it was failing. The glyph and text are muted while the card
    // runs; opened, the row is still the error surface with the hand-off. A
    // card that COMPLETED keeps them muted -- the agent finished with them
    // handled, and "Complete" over red rows read as a contradiction -- and the
    // card that ends in FAILURE shows them red. The SHAPE says failed in every
    // state (a circled X, not the circled "!" that reads as a note while
    // grey), so the card's end changes emphasis, not meaning.
    const transcript = [
      { kind: 'tool' as const, seq: 0, id: 'tc-1', tool: 'Running: npm test', output: 'FAIL src/a.test.ts\n  exit code 1', status: 'failed' },
      { kind: 'tool' as const, seq: 1, id: 'tc-2', tool: 'Reading a.ts', output: 'const a = 1', status: 'completed' },
    ]
    const { unmount } = renderPanel(panel(mkAgent('native:c1', { status: 'tool', transcript })))
    const [failed] = screen.getAllByTestId('subagent-transcript-tool')
    const glyph = within(failed).getByLabelText('Error')
    expect(glyph.getAttribute('class')).not.toContain('text-danger')
    expect(glyph.getAttribute('class')).toContain('text-muted')
    expect(glyph.getAttribute('class')).toContain('lucide-circle-x')
    expect(glyph.getAttribute('class')).not.toContain('lucide-circle-alert')
    expect(failed.querySelector('.text-danger\\/80')).toBeNull()
    fireEvent.click(within(failed).getByRole('button', { name: 'npm test' }))
    const notice = within(failed).getByTestId('subagent-transcript-tool-error')
    expect(notice).toHaveTextContent('Tool call failed')
    expect(within(notice).getByText('Ask the agent about this failed call')).toBeInTheDocument()
    unmount()

    const completed = renderPanel(panel(mkAgent('native:c1', { status: 'done', result: 'fixed the test', transcript })))
    // Finished cards collapse by default; open the card.
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    const [onComplete] = screen.getAllByTestId('subagent-transcript-tool')
    expect(within(onComplete).getByLabelText('Error').getAttribute('class')).not.toContain('text-danger')
    expect(within(onComplete).getByLabelText('Error').getAttribute('class')).toContain('text-muted')
    expect(within(onComplete).getByLabelText('Error').getAttribute('class')).toContain('lucide-circle-x')
    expect(onComplete.querySelector('.text-danger\\/80')).toBeNull()
    completed.unmount()

    renderPanel(panel(mkAgent('native:c1', { status: 'error', error: 'gave up', transcript })))
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    const [ended] = screen.getAllByTestId('subagent-transcript-tool')
    expect(within(ended).getByLabelText('Error').getAttribute('class')).toContain('text-danger')
    expect(within(ended).getByLabelText('Error').getAttribute('class')).toContain('lucide-circle-x')
  })

  it('renders its disclosures through the shared button primitive', () => {
    // The reasoning, tool-row and (finished card) full-result toggles are
    // Btn, not raw buttons: they keep its pressed-state behaviour and styling
    // while overriding the chrome a text-link disclosure cannot carry.
    renderPanel(panel(live))
    const thinking = within(screen.getByTestId('subagent-transcript-thinking')).getByRole('button')
    const [tool] = screen.getAllByTestId('subagent-transcript-tool')
    for (const b of [thinking, within(tool).getByRole('button')]) {
      expect(b.className).toContain('active:scale-[0.97]')
      expect(b.className).toContain('!border-none')
    }
  })

  it('folds reasoning until asked, labelled by whether it is still live', () => {
    renderPanel(panel(live))
    const row = screen.getByTestId('subagent-transcript-thinking')
    expect(within(row).getByText('Thought process')).toBeInTheDocument()
    // Folded, the reasoning shows only as the row's one-line preview.
    expect(row.querySelector('pre')).toBeNull()
    expect(within(row).getByTestId('subagent-transcript-thinking-preview')).toHaveTextContent('plan the review')
    fireEvent.click(within(row).getByRole('button', { name: 'Show reasoning' }))
    expect(row.querySelector('pre')).toHaveTextContent('plan the review')
    expect(within(row).queryByTestId('subagent-transcript-thinking-preview')).toBeNull()
  })

  it('an opened reasoning row stays the opened one when the cap drops rows off the front', () => {
    // Rows are keyed by their `seq`, not their position: once the cap is
    // reached every new row shifts the positions down by one, and a position
    // key would hand the opened row's state to the row that slid into its place.
    const before = mkAgent('s1', {
      transcript: [
        { kind: 'thinking', seq: 10, text: 'first thought' },
        { kind: 'thinking', seq: 11, text: 'second thought' },
      ],
    })
    const { rerender } = renderPanel(panel(before))
    const [first, second] = screen.getAllByTestId('subagent-transcript-thinking')
    fireEvent.click(within(second).getByRole('button', { name: 'Show reasoning' }))
    expect(second.querySelector('pre')).toHaveTextContent('second thought')
    expect(first.querySelector('pre')).toBeNull()

    // The cap drops the first row; a new one arrives at the end.
    rerender(panel(mkAgent('s1', {
      transcript: [
        { kind: 'thinking', seq: 11, text: 'second thought' },
        { kind: 'thinking', seq: 12, text: 'third thought' },
      ],
      transcriptOmitted: 1,
    })))

    const [kept, added] = screen.getAllByTestId('subagent-transcript-thinking')
    expect(kept.querySelector('pre')).toHaveTextContent('second thought')
    expect(added.querySelector('pre')).toBeNull()
  })

  it('names a call first seen through its result until its refinement arrives', () => {
    // The reducer opens a titleless row for a result whose call it never saw
    // open; the row says what it is rather than standing blank.
    renderPanel(panel(mkAgent('s1', { status: 'tool', transcript: [{ kind: 'tool', seq: 0, id: 'tc-1', tool: '', output: 'ok', status: 'completed' }] })))
    const row = screen.getByTestId('subagent-transcript-tool')
    // Under the "Live activity" heading a bare "Tool call" read like a heading;
    // the row says that its name has not arrived.
    expect(row).toHaveTextContent('Unnamed tool call')
    expect(within(row).getByRole('button', { name: 'Unnamed tool call' })).toHaveAttribute('title', 'Unnamed tool call')
  })

  it('previews the first line of folded reasoning, so alike rows can be told apart', () => {
    // Three folded rows all read "Thought process"; the preview is what says
    // which is which without opening each. It is content, not a label, so it
    // sits outside the button's accessible name and goes when the row opens.
    renderPanel(panel(mkAgent('native:c1', {
      status: 'done',
      result: 'done',
      transcript: [
        { kind: 'thinking', seq: 0, text: '\n  plan the review  \nthen the diff' },
        { kind: 'thinking', seq: 1, text: 'weigh the two fixes' },
      ],
    })))
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    const [first, second] = screen.getAllByTestId('subagent-transcript-thinking')
    const preview = within(first).getByTestId('subagent-transcript-thinking-preview')
    expect(preview).toHaveTextContent('plan the review')
    expect(preview).not.toHaveTextContent('then the diff')
    expect(preview.getAttribute('class')).toContain('truncate')
    expect(preview).toHaveAttribute('aria-hidden')
    expect(within(second).getByTestId('subagent-transcript-thinking-preview')).toHaveTextContent('weigh the two fixes')
    expect(within(first).getByRole('button', { name: 'Show reasoning' })).toBeInTheDocument()
    fireEvent.click(within(first).getByRole('button', { name: 'Show reasoning' }))
    expect(within(first).queryByTestId('subagent-transcript-thinking-preview')).toBeNull()
    expect(within(first).getByText(/then the diff/)).toBeInTheDocument()
  })

  it('labels the newest reasoning row as live while the agent runs', () => {
    renderPanel(panel(mkAgent('s1', { transcript: [{ kind: 'thinking', seq: 0, text: 'still going' }] })))
    expect(within(screen.getByTestId('subagent-transcript-thinking')).getByText('Thinking')).toBeInTheDocument()
  })

  it('says how many earlier steps the cap dropped', () => {
    renderPanel(panel(mkAgent('s1', { transcript: [{ kind: 'text', seq: 0, text: 'tail' }], transcriptOmitted: 3 })))
    expect(screen.getByTestId('subagent-transcript-omitted')).toHaveTextContent('3 earlier steps not shown')
  })

  it('folds the final Output box on a finished card that has rows, and opens it on request', () => {
    // The rows already carry every tool output and the answer; the done-time
    // result repeats them under the calls' titles, so the box starts folded.
    renderPanel(panel(mkAgent('native:c1', {
      status: 'done',
      result: '→ Reading a.ts\nconst a = 1\nfinal answer',
      transcript: [
        { kind: 'tool', seq: 0, id: 'tc-1', tool: 'Reading a.ts', output: 'const a = 1', status: 'completed' },
        { kind: 'text', seq: 1, text: 'final answer' },
      ],
    })))
    // Finished cards collapse by default; open the card.
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(screen.getByTestId('subagent-transcript')).toBeInTheDocument()
    // The run is over, so the rows are what it did, not what it is doing.
    expect(screen.getByTestId('subagent-transcript-heading')).toHaveTextContent('Activity')
    expect(screen.getByTestId('subagent-transcript-heading')).not.toHaveTextContent('Live activity')
    expect(screen.queryByTestId('subagent-output-body')).not.toBeInTheDocument()
    const toggle = screen.getByTestId('subagent-output-toggle')
    // The block the parent received must not hide behind the faintest control
    // on the card: the toggle carries the tool-row labels' weight and case.
    expect(toggle.className).toContain('text-muted/80')
    expect(toggle.className).not.toContain('uppercase')
    expect(toggle.className).not.toContain('text-[10px]')
    expect(toggle).toHaveTextContent('Full result')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle.className).toContain('active:scale-[0.97]')
    // Named for what it holds -- the one block the parent received -- so it is
    // not a second "Output" beside rows that already carry each output.
    expect(toggle).toHaveTextContent('Full result')
    expect(toggle).not.toHaveTextContent(/^Output/)
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('subagent-output-body')).toHaveTextContent('final answer')
    fireEvent.click(toggle)
    expect(screen.queryByTestId('subagent-output-body')).not.toBeInTheDocument()
  })

  it('shows the Output box plainly on a finished card without rows', () => {
    renderPanel(panel(mkAgent('native:c1', { status: 'done', result: 'final answer' })))
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(screen.queryByTestId('subagent-output-toggle')).not.toBeInTheDocument()
    expect(screen.getByTestId('subagent-output-body')).toHaveTextContent('final answer')
  })

  it('leaves a card without rows exactly as before', () => {
    renderPanel(panel(mkAgent('s1', { status: 'tool', lastTool: 'fs_read', streaming: 'partial' })))
    expect(screen.queryByTestId('subagent-transcript')).not.toBeInTheDocument()
    expect(screen.getByTestId('subagent-output-body')).toHaveTextContent('partial')
    expect(screen.getByTestId('subagent-output-body')).toHaveTextContent('fs_read')
  })
})
