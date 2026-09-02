/**
 * Coverage suite for `ProjectDetailPage` — everything the smoke suite
 * (`ProjectDetailPage.test.tsx`) leaves cold.
 *
 * That suite pins the static surface: tab bar, view toggle, idea fallback, the
 * export button. What was untested is the whole write side, which is where a
 * silent failure loses a user's plan edits or strands a run behind an approval
 * gate:
 *
 *  - the approvals poll and its `task-gate-<n>-` id parser,
 *  - the approval banner (rendered, suppressed, and its go-to-task jump),
 *  - approve/reject from the DAG and from the detail panel,
 *  - the requires_approval / force_approval toggle, including its
 *    response-shape interpretation and its failure branch,
 *  - the two save paths (single-task PATCH for live runs, whole-plan PUT for
 *    stopped ones) plus override merging and the error branch,
 *  - pending-edit bookkeeping and the reset-on-run-change effect,
 *  - `PlanningOverlay`'s ticking dots.
 *
 * Harness notes, all deliberate:
 *  1. The three `aidlc` children are replaced by stubs that expose each
 *     callback as a button and each interesting prop as a data attribute. The
 *     real DagView lays out with measured geometry and TaskDetailPanel owns its
 *     own edit form; neither is under test here, and going through them would
 *     make these assertions about their internals instead of this page's.
 *  2. framer-motion renders as plain DOM so the detail panel mounts and
 *     unmounts synchronously — a real exit transition would make
 *     "the selection cleared" pass or fail on timing.
 *  3. The api client is automocked; every test sets the responses it needs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { ProjectRun, TaskDetail } from '../types'

vi.mock('../api/client')

/* Render framer-motion elements as plain DOM (see harness note 2). */
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

/** Update payload the stub panel sends on its next save / edit click. */
let nextSave = { title: 'Renamed', description: 'New body', depends_on: [] as number[] }
/** Resolved values of every `onToggleApproval` call, oldest first. */
const toggleResults: boolean[] = []
/** Resolved override (JSON) or `err:<reason>` for every `onSave` call. */
const saveResults: string[] = []

interface DagNodeStub { id: string; title: string; status: string; requires_approval?: boolean }

vi.mock('../pages/aidlc/DagView', () => ({
  default: ({ nodes, edges, onNodeClick, selectedId, pendingEditIds, approvalMap, onApprove }: {
    nodes: DagNodeStub[]
    edges: { from: string; to: string }[]
    onNodeClick: (id: string) => void
    selectedId?: string
    pendingEditIds?: Set<string>
    approvalMap?: Record<number, string>
    onApprove?: (index: number, decision: 'approve' | 'reject') => void
  }) => (
    <div
      data-testid="dag-view"
      data-selected={selectedId ?? ''}
      data-pending={[...(pendingEditIds ?? [])].sort().join(',')}
      data-approvals={approvalMap ? Object.keys(approvalMap).sort().join(',') : 'none'}
      data-edges={edges.map(e => `${e.from}>${e.to}`).sort().join(',')}
    >
      {nodes.map(n => (
        <div key={n.id}>
          <span>{`title-${n.id}:${n.title}`}</span>
          {/* One action per row. AUTOSDE `max-two-buttons-per-row` is blocking and
              its file-patterns match .tsx files under src, test files included, so
              even a vi.mock stub cannot park three sibling actions in one group.
              Each button is only a callback entry point here, so the extra
              wrappers cost the test nothing. */}
          <div>
            <button type="button" onClick={() => onNodeClick(n.id)}>{`pick-${n.id}`}</button>
          </div>
          <div>
            <button
              type="button"
              onClick={() => onApprove?.(Number(n.id), 'approve')}
            >{`dag-ok-${n.id}`}</button>
          </div>
          <div>
            <button
              type="button"
              onClick={() => onApprove?.(Number(n.id), 'reject')}
            >{`dag-no-${n.id}`}</button>
          </div>
        </div>
      ))}
    </div>
  ),
}))

vi.mock('../pages/aidlc/PhasedView', () => ({
  default: ({ tasks, onTaskClick, selectedIndex, pendingEditIndexes }: {
    tasks: TaskDetail[]
    onTaskClick?: (index: number) => void
    selectedIndex?: number | null
    pendingEditIndexes?: Set<number>
  }) => (
    <div
      data-testid="phased-view"
      data-selected={selectedIndex ?? ''}
      data-pending={[...(pendingEditIndexes ?? [])].sort().join(',')}
    >
      {tasks.map(t => (
        <div key={t.index}>
          <button type="button" onClick={() => onTaskClick?.(t.index)}>{`phase-pick-${t.index}`}</button>
        </div>
      ))}
    </div>
  ),
}))

vi.mock('../pages/aidlc/TaskDetailPanel', () => ({
  default: ({ task, allTasks, onClose, onApprove, onToggleApproval, editable, onSave, pendingEdits, onEdit }: {
    task: TaskDetail
    allTasks?: TaskDetail[]
    onClose: () => void
    onApprove?: (decision: 'approve' | 'reject') => void
    onToggleApproval?: (index: number, field: 'requires_approval' | 'force_approval', value: boolean) => Promise<boolean>
    editable?: boolean
    onSave?: (index: number, updates: { title: string; description: string; depends_on: number[] }) => Promise<{ title: string; description: string; depends_on: number[] } | void>
    pendingEdits?: Record<number, { title: string; description: string; depends_on: number[] }>
    onEdit?: (index: number, updates?: { title: string; description: string; depends_on: number[] }) => void
  }) => (
    <div
      data-testid="task-panel"
      data-editable={String(!!editable)}
      data-all-tasks={String((allTasks ?? []).length)}
      data-pending={Object.keys(pendingEdits ?? {}).sort().join(',')}
    >
      <span>{`panel-task:${task.index}:${task.title}`}</span>
      <div>
        <button type="button" onClick={onClose}>panel-close</button>
      </div>
      {onApprove && (
        <>
          <div>
            <button type="button" onClick={() => onApprove('approve')}>panel-approve</button>
          </div>
          <div>
            <button type="button" onClick={() => onApprove('reject')}>panel-reject</button>
          </div>
        </>
      )}
      {onToggleApproval && (
        <>
          <div>
            <button
              type="button"
              onClick={() => { void onToggleApproval(task.index, 'requires_approval', false).then(r => { toggleResults.push(r) }) }}
            >toggle-req-off</button>
          </div>
          <div>
            <button
              type="button"
              onClick={() => { void onToggleApproval(task.index, 'force_approval', true).then(r => { toggleResults.push(r) }) }}
            >toggle-force-on</button>
          </div>
        </>
      )}
      <div>
        <button
          type="button"
          onClick={() => {
            void onSave?.(task.index, nextSave)
              .then(r => { saveResults.push(JSON.stringify(r ?? null)) })
              .catch((e: unknown) => { saveResults.push(`err:${e instanceof Error ? e.message : String(e)}`) })
          }}
        >panel-save</button>
      </div>
      <div>
        <button type="button" onClick={() => onEdit?.(task.index, nextSave)}>panel-edit</button>
      </div>
      <div>
        <button type="button" onClick={() => onEdit?.(task.index, undefined)}>panel-unedit</button>
      </div>
    </div>
  ),
}))

import ProjectDetailPage from '../pages/ProjectDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

const step = (over: Partial<TaskDetail> = {}): TaskDetail => ({
  index: 1, title: 'Setup', description: 'Init', status: 'pending', error: '', result: '',
  attempts: 0, depends_on: [], requires_approval: false, ...over,
})

const mockRun = (overrides: Partial<ProjectRun> = {}): ProjectRun => ({
  task_id: 'run-1', name: 'Test Run', running: false, status: 'planned',
  steps: 3, completed: 0, failed: 0, skipped: 0, current_step: 0,
  spec: 'test.md', spec_name: 'Test', error: '',
  tokens_used: 0, replan_count: 0,
  started_at: 1_700_000_000, finished_at: 0,
  work_dir: '/tmp/test', branch_name: 'trunk', spec_content: '# Test spec',
  lessons_learned: [], commits: 0, original_input: 'test input', source: 'text',
  groups: [[1, 2], [3]],
  task_details: [
    step({ index: 1, title: 'Setup', description: 'Init' }),
    step({ index: 2, title: 'Build', description: 'Compile', depends_on: [1] }),
    step({ index: 3, title: 'Verify', description: 'Check', depends_on: [1, 2] }),
  ],
  ...overrides,
})

/** An approvals payload gating `indexes`, plus one id the parser must ignore. */
const gates = (...indexes: number[]) => [
  { id: 'tool-call-9', source: 'chat' },
  ...indexes.map(i => ({ id: `task-gate-${i}-abc`, source: 'taskrunner' })),
]

const dag = () => screen.getByTestId('dag-view')

/* PlanningOverlay runs a 500ms setInterval, and the approvals query polls every
 * 3s. Both outlive a test that ends mid-tick; on a torn-down environment their
 * callbacks throw "window is not defined" as an UNHANDLED error, which reddens
 * CI with every test still reported as passing. Fake timers keep them off the
 * real clock and clearAllTimers drops the pending ones at teardown;
 * shouldAdvanceTime keeps waitFor and the polls behaving as they do live. */
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  toggleResults.length = 0
  saveResults.length = 0
  nextSave = { title: 'Renamed', description: 'New body', depends_on: [] }
  vi.mocked(api.approvals).mockResolvedValue([])
  // ThemeProvider boots through the same automocked client; without a value its
  // query logs "Query data cannot be undefined" on every render.
  vi.mocked(api.themeBoot).mockResolvedValue({})
  vi.mocked(api.resolveApproval).mockResolvedValue({ ok: true })
  vi.mocked(api.updateTask).mockResolvedValue({ ok: true, title: 'Renamed', description: 'New body', depends_on: [] })
  vi.mocked(api.updatePlan).mockResolvedValue({ steps: [] })
  vi.mocked(api.exportPlanYaml).mockResolvedValue(undefined)
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('ProjectDetailPage approvals poll and banner', () => {
  it('parses task-gate ids into a per-task map and ignores unrelated approvals', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(2))
    renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'running', running: true })} />)
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '2'), { timeout: 5_000 })
  })

  it('does not poll approvals or pass a map to the DAG for a stopped run', async () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'planned' })} />)
    expect(dag()).toHaveAttribute('data-approvals', 'none')
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
    expect(api.approvals).not.toHaveBeenCalled()
  })

  it('banners the first gated task that is actually in progress', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(2))
    renderWithProviders(
      <ProjectDetailPage run={mockRun({
        status: 'running',
        task_details: [step({ index: 1 }), step({ index: 2, title: 'Build', status: 'in_progress' })],
      })} />,
    )
    expect(await screen.findByRole('button', { name: /Go to Task/ }, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.getByText(/Task 2 .*is waiting for your decision/)).toBeInTheDocument()
  })

  it('suppresses the banner when the gated task has not reached in_progress', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(2))
    renderWithProviders(
      <ProjectDetailPage run={mockRun({
        status: 'running',
        task_details: [step({ index: 1 }), step({ index: 2, title: 'Build', status: 'pending' })],
      })} />,
    )
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '2'), { timeout: 5_000 })
    expect(screen.queryByRole('button', { name: /Go to Task/ })).not.toBeInTheDocument()
  })

  it('jumps to the gated task from the banner', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(2))
    renderWithProviders(
      <ProjectDetailPage run={mockRun({
        status: 'running',
        task_details: [step({ index: 1 }), step({ index: 2, title: 'Build', status: 'in_progress' })],
      })} />,
    )
    fireEvent.click(await screen.findByRole('button', { name: /Go to Task/ }, { timeout: 5_000 }))
    expect(screen.getByText('panel-task:2:Build')).toBeInTheDocument()
    expect(dag()).toHaveAttribute('data-selected', '2')
  })
})

describe('ProjectDetailPage approve and reject', () => {
  const runningRun = (over: Partial<ProjectRun> = {}) => mockRun({
    status: 'running',
    running: true,
    task_details: [
      step({ index: 1, title: 'Setup', status: 'in_progress' }),
      step({ index: 2, title: 'Build', status: 'pending' }),
    ],
    ...over,
  })

  it('resolves a gate approved from the DAG and refreshes the run', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(1))
    const onRefresh = vi.fn()
    renderWithProviders(<ProjectDetailPage run={runningRun()} onRefresh={onRefresh} />)
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '1'), { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'dag-ok-1' }))
    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('task-gate-1-abc', 'approve'), { timeout: 5_000 })
    await waitFor(() => expect(onRefresh).toHaveBeenCalled(), { timeout: 5_000 })
  })

  it('opens the rejected task in the panel so the user can see why', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(1))
    renderWithProviders(<ProjectDetailPage run={runningRun()} />)
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '1'), { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'dag-no-1' }))
    expect(await screen.findByText('panel-task:1:Setup', undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(api.resolveApproval).toHaveBeenCalledWith('task-gate-1-abc', 'reject')
  })

  it('makes no request when the DAG approves a task that has no gate', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(1))
    renderWithProviders(<ProjectDetailPage run={runningRun()} />)
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '1'), { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'dag-ok-2' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('resolves the gate from the detail panel and keeps it open on reject', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(1))
    renderWithProviders(<ProjectDetailPage run={runningRun()} />)
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '1'), { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-approve' }, { timeout: 5_000 }))
    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('task-gate-1-abc', 'approve'), { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'panel-reject' }))
    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('task-gate-1-abc', 'reject'), { timeout: 5_000 })
    expect(screen.getByTestId('task-panel')).toBeInTheDocument()
  })

  it('wires no approve action into the panel for an ungated task', async () => {
    vi.mocked(api.approvals).mockResolvedValue(gates(1))
    renderWithProviders(<ProjectDetailPage run={runningRun()} />)
    await waitFor(() => expect(dag()).toHaveAttribute('data-approvals', '1'), { timeout: 5_000 })
    fireEvent.click(screen.getByRole('button', { name: 'pick-2' }))
    expect(await screen.findByTestId('task-panel', undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'panel-approve' })).not.toBeInTheDocument()
  })
})

describe('ProjectDetailPage approval toggles', () => {
  const openPanel = async (run: ProjectRun) => {
    const rendered = renderWithProviders(<ProjectDetailPage run={run} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    await screen.findByTestId('task-panel', undefined, { timeout: 5_000 })
    return rendered
  }

  it('clears force_approval alongside requires_approval and reports success', async () => {
    const onRefresh = vi.fn()
    renderWithProviders(<ProjectDetailPage run={mockRun()} onRefresh={onRefresh} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'toggle-req-off' }, { timeout: 5_000 }))
    await waitFor(() => expect(toggleResults).toEqual([true]), { timeout: 5_000 })
    expect(api.updateTask).toHaveBeenCalledWith('run-1', 1, { requires_approval: false, force_approval: false })
    expect(onRefresh).toHaveBeenCalled()
  })

  it('sends force_approval on its own, and treats an ok-less response as success', async () => {
    vi.mocked(api.updateTask).mockResolvedValue({ index: 1 })
    await openPanel(mockRun())
    fireEvent.click(screen.getByRole('button', { name: 'toggle-force-on' }))
    await waitFor(() => expect(toggleResults).toEqual([true]), { timeout: 5_000 })
    expect(api.updateTask).toHaveBeenCalledWith('run-1', 1, { force_approval: true })
  })

  it('reports failure when the server answers ok: false', async () => {
    vi.mocked(api.updateTask).mockResolvedValue({ ok: false })
    await openPanel(mockRun())
    fireEvent.click(screen.getByRole('button', { name: 'toggle-force-on' }))
    await waitFor(() => expect(toggleResults).toEqual([false]), { timeout: 5_000 })
  })

  it('reports failure when the request itself fails', async () => {
    vi.mocked(api.updateTask).mockRejectedValue(new Error('patch refused'))
    await openPanel(mockRun())
    fireEvent.click(screen.getByRole('button', { name: 'toggle-force-on' }))
    await waitFor(() => expect(toggleResults).toEqual([false]), { timeout: 5_000 })
  })

  it('withholds the toggle for a run whose plan can no longer change', async () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'completed' })} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    const panel = await screen.findByTestId('task-panel', undefined, { timeout: 5_000 })
    expect(panel).toHaveAttribute('data-editable', 'false')
    expect(screen.queryByRole('button', { name: 'toggle-force-on' })).not.toBeInTheDocument()
  })

  it('keeps a live run editable only while the task is still pending', async () => {
    const live = mockRun({
      status: 'running',
      task_details: [step({ index: 1, status: 'in_progress' }), step({ index: 2, title: 'Build', status: 'pending' })],
    })
    renderWithProviders(<ProjectDetailPage run={live} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    expect(await screen.findByTestId('task-panel', undefined, { timeout: 5_000 })).toHaveAttribute('data-editable', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'pick-2' }))
    await waitFor(() => expect(screen.getByTestId('task-panel')).toHaveAttribute('data-editable', 'true'), { timeout: 5_000 })
  })
})

describe('ProjectDetailPage saving task edits', () => {
  it('patches a single task for a live run and shows the returned title', async () => {
    const live = mockRun({ status: 'running', task_details: [step({ index: 1, status: 'pending' })] })
    vi.mocked(api.updateTask).mockResolvedValue({ title: 'Server title', description: 'Server body', depends_on: [] })
    renderWithProviders(<ProjectDetailPage run={live} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-save' }, { timeout: 5_000 }))
    await waitFor(() => expect(api.updateTask).toHaveBeenCalledWith('run-1', 1, nextSave), { timeout: 5_000 })
    await waitFor(() => expect(screen.getByText('title-1:Server title')).toBeInTheDocument(), { timeout: 5_000 })
    expect(saveResults).toEqual([JSON.stringify({ title: 'Server title', description: 'Server body', depends_on: [] })])
  })

  it('rewrites the whole plan for a stopped run and folds earlier saves into the payload', async () => {
    vi.mocked(api.updatePlan).mockImplementation(async (_id: string, steps: unknown) => ({
      steps: (steps as { title: string; description: string; depends_on: number[] }[]).map((s, i) => ({
        index: i + 1, title: s.title, description: s.description, depends_on: s.depends_on,
      })),
    }))
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)

    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-save' }, { timeout: 5_000 }))
    await waitFor(() => expect(screen.getByText('title-1:Renamed')).toBeInTheDocument(), { timeout: 5_000 })

    // Second save on a different task: the payload must still carry task 1's
    // saved override, otherwise the first edit is silently rolled back.
    nextSave = { title: 'Second', description: 'Second body', depends_on: [1] }
    fireEvent.click(screen.getByRole('button', { name: 'pick-2' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-save' }, { timeout: 5_000 }))
    await waitFor(() => expect(screen.getByText('title-2:Second')).toBeInTheDocument(), { timeout: 5_000 })

    expect(api.updatePlan).toHaveBeenCalledTimes(2)
    const secondPayload = vi.mocked(api.updatePlan).mock.calls[1][1] as { title: string }[]
    expect(secondPayload.map(s => s.title)).toEqual(['Renamed', 'Second', 'Verify'])
    expect(screen.getByText('title-1:Renamed')).toBeInTheDocument()
  })

  it('keeps the local edit when the plan response omits the saved step', async () => {
    vi.mocked(api.updatePlan).mockResolvedValue({})
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-save' }, { timeout: 5_000 }))
    await waitFor(() => expect(screen.getByText('title-1:Renamed')).toBeInTheDocument(), { timeout: 5_000 })
    expect(saveResults).toEqual([JSON.stringify(nextSave)])
  })

  it('surfaces a failed plan rewrite to the caller instead of swallowing it', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(api.updatePlan).mockRejectedValue(new Error('plan locked'))
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-save' }, { timeout: 5_000 }))
    await waitFor(() => expect(saveResults).toEqual(['err:plan locked']), { timeout: 5_000 })
    expect(err).toHaveBeenCalled()
    expect(screen.getByText('title-1:Setup')).toBeInTheDocument()
    err.mockRestore()
  })
})

describe('ProjectDetailPage pending edits and selection', () => {
  it('tracks a pending edit for both views and drops it when it is reverted', async () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-2' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-edit' }, { timeout: 5_000 }))
    await waitFor(() => expect(dag()).toHaveAttribute('data-pending', '2'), { timeout: 5_000 })
    expect(screen.getByTestId('task-panel')).toHaveAttribute('data-pending', '2')

    fireEvent.click(screen.getByRole('button', { name: 'Phased' }))
    expect(screen.getByTestId('phased-view')).toHaveAttribute('data-pending', '2')

    fireEvent.click(screen.getByRole('button', { name: 'panel-unedit' }))
    await waitFor(() => expect(screen.getByTestId('phased-view')).toHaveAttribute('data-pending', ''), { timeout: 5_000 })
  })

  it('selects from the phased view and closes from the panel', async () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Phased' }))
    fireEvent.click(screen.getByRole('button', { name: 'phase-pick-3' }))
    expect(await screen.findByText('panel-task:3:Verify', undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.getByTestId('phased-view')).toHaveAttribute('data-selected', '3')
    expect(screen.getByTestId('task-panel')).toHaveAttribute('data-all-tasks', '3')

    fireEvent.click(screen.getByRole('button', { name: 'panel-close' }))
    await waitFor(() => expect(screen.queryByTestId('task-panel')).not.toBeInTheDocument(), { timeout: 5_000 })
  })

  it('clears the selection and pending edits when the run changes', async () => {
    const { rerender } = renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'pick-1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'panel-edit' }, { timeout: 5_000 }))
    await waitFor(() => expect(dag()).toHaveAttribute('data-pending', '1'), { timeout: 5_000 })

    rerender(<ProjectDetailPage run={mockRun({ task_id: 'run-2' })} />)
    await waitFor(() => expect(screen.queryByTestId('task-panel')).not.toBeInTheDocument(), { timeout: 5_000 })
    expect(dag()).toHaveAttribute('data-pending', '')
  })

  it('feeds the DAG one edge per dependency', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    expect(dag()).toHaveAttribute('data-edges', '1>2,1>3,2>3')
  })
})

describe('ProjectDetailPage idea tab and export', () => {
  it('hands the idea to the chat composer', () => {
    const { store } = renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Idea' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit in Chat' }))
    expect(store.getState().chat.pendingInput).toBe('# Test spec')
  })

  it('returns from the idea tab to the DAG after a detour through the phased view', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Phased' }))
    fireEvent.click(screen.getByRole('button', { name: 'DAG' }))
    expect(screen.getByTestId('dag-view')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Idea' }))
    fireEvent.click(screen.getByRole('button', { name: 'Tasks' }))
    expect(screen.getByTestId('dag-view')).toBeInTheDocument()
  })

  it('logs an export failure and re-enables the button', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(api.exportPlanYaml).mockRejectedValue(new Error('no plan'))
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByRole('button', { name: /Export YAML/ }))
    await waitFor(() => expect(err).toHaveBeenCalled(), { timeout: 5_000 })
    await waitFor(() => expect(screen.getByRole('button', { name: /Export YAML/ })).toBeEnabled(), { timeout: 5_000 })
    err.mockRestore()
  })
})

describe('ProjectDetailPage planning overlay', () => {
  it('ticks the dots and hides the view toggle while planning', async () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'planning' })} />)
    const line = screen.getByText(/Generating execution plan/)
    expect(line.textContent).toBe('Generating execution plan')
    expect(screen.queryByRole('button', { name: 'DAG' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Export YAML/ })).not.toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(line.textContent).toMatch(/^Generating execution plan\.{1,3}$/)

    // Four more ticks are guaranteed to cross the reset branch (dots >= 3 -> '').
    await act(async () => { await vi.advanceTimersByTimeAsync(2_100) })
    expect(line.textContent).toMatch(/^Generating execution plan\.{0,3}$/)
  })
})
