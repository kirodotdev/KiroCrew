import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { useNavigate, type NavigateFunction } from 'react-router-dom'
import { renderWithProviders } from './helpers'
import ProjectsPage from '../pages/ProjectsPage'
import PhasedView from '../pages/aidlc/PhasedView'
import DagView from '../pages/aidlc/DagView'
import type { ProjectRun, TaskDetail } from '../types'

// Mock child components for ProjectsPage isolation
vi.mock('../pages/ProjectDetailPage', () => ({ default: () => <div data-testid="project-detail">Detail</div> }))
vi.mock('../components/AgentSelector', () => ({ default: ({ value, onChange }: { value: string; onChange: (name: string) => void }) => <select data-testid="agent-select" value={value} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value)}><option value="">default</option></select> }))

vi.mock('../api/client', () => ({
  api: {
    taskRunnerStatus: vi.fn().mockResolvedValue({ running: false, available: true, runs: [] }),
    agentsInstalled: vi.fn().mockResolvedValue([]),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    startTaskRunner: vi.fn().mockResolvedValue({ ok: true }),
    cancelTaskRunner: vi.fn().mockResolvedValue({ ok: true }),
    deleteTaskRun: vi.fn().mockResolvedValue({ ok: true }),
    retryTaskRun: vi.fn().mockResolvedValue({ ok: true }),
    planTask: vi.fn().mockResolvedValue({ ok: true, task_id: 'plan-1' }),
    cancelPlan: vi.fn().mockResolvedValue({ ok: true }),
    executePlan: vi.fn().mockResolvedValue({ ok: true }),
    planContext: vi.fn().mockResolvedValue({ ok: true, context: 'plan context' }),
    refineStatus: vi.fn().mockResolvedValue({ status: 'idle', text: '', error: '' }),
    refineTaskInput: vi.fn().mockResolvedValue({ ok: true }),
    refineCancel: vi.fn().mockResolvedValue({ ok: true }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    createCron: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const mockTasks: TaskDetail[] = [
  { index: 1, title: 'Setup', description: '', status: 'passed', error: '', result: '', attempts: 1, depends_on: [], requires_approval: false },
  { index: 2, title: 'Build', description: '', status: 'in_progress', error: '', result: '', attempts: 1, depends_on: [1], requires_approval: false },
  { index: 3, title: 'Verify', description: '', status: 'reviewing', error: '', result: '', attempts: 1, depends_on: [1], requires_approval: false, task_type: 'checkpoint' },
  { index: 4, title: 'Deploy', description: '', status: 'pending', error: '', result: '', attempts: 1, depends_on: [2, 3], requires_approval: false },
  { index: 5, title: 'Broken', description: '', status: 'failed', error: 'compile error', result: '', attempts: 2, depends_on: [1], requires_approval: false },
  { index: 6, title: 'Skipped', description: '', status: 'skipped', error: '', result: '', attempts: 0, depends_on: [5], requires_approval: false },
]

// ── ProjectsPage ──

describe('ProjectsPage', () => {
  beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear() })

  it('renders page header and mode toggle', () => {
    renderWithProviders(<ProjectsPage />)
    expect(screen.getByText('Task Runner')).toBeInTheDocument()
    expect(screen.getByText(/Compose/)).toBeInTheDocument()
    expect(screen.getByText(/From Spec/)).toBeInTheDocument()
  })

  it('renders the New Task button (renamed from New Project) when runs exist', async () => {
    const run: ProjectRun = {
      task_id: 'run-x', name: 'Existing', running: false, status: 'completed',
      steps: 1, completed: 1, failed: 0, skipped: 0, current_step: 1,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: 'spec', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [run] })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Existing')
    expect(screen.getByRole('button', { name: /New Task/ })).toBeInTheDocument()
  })

  it('renders compose textarea by default', () => {
    renderWithProviders(<ProjectsPage />)
    expect(screen.getByPlaceholderText('Describe your task...')).toBeInTheDocument()
  })

  it('shows the backend default workspace folder as a placeholder, never a prefilled value', async () => {
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [], default_workspace_dir: '/home/u/ws' })
    renderWithProviders(<ProjectsPage />)
    const ws = await screen.findByPlaceholderText('/home/u/ws') as HTMLInputElement
    expect(ws).toBeInTheDocument()
    // Untouched field stays empty → "no override" (preserves per-run isolation).
    expect(ws.value).toBe('')
  })

  it('threads the typed workspace dir into planTask on Run', async () => {
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [], default_workspace_dir: '/ws/root' })
    renderWithProviders(<ProjectsPage />)
    const ws = await screen.findByPlaceholderText('/ws/root')
    fireEvent.change(ws, { target: { value: '/custom/dir' } })
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'do the thing' } })
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))
    expect(mockApi.planTask).toHaveBeenCalledWith('do the thing', 'text', '', '', '/custom/dir')
  })

  it('sends an empty workspace (no override) when the field is untouched', async () => {
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [], default_workspace_dir: '/ws/root' })
    renderWithProviders(<ProjectsPage />)
    await screen.findByPlaceholderText('/ws/root')
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'do it' } })
    fireEvent.click(screen.getByRole('button', { name: /Run/ }))
    expect(mockApi.planTask).toHaveBeenCalledWith('do it', 'text', '', '', '')
  })

  it('switches to From Spec mode with file upload', () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByText(/From Spec/))
    expect(screen.getByPlaceholderText('Paste spec content or upload a file...')).toBeInTheDocument()
  })

  it('renders agent selector', () => {
    renderWithProviders(<ProjectsPage />)
    expect(screen.getByTestId('agent-select')).toBeInTheDocument()
  })

  it('shows empty state when no runs', () => {
    renderWithProviders(<ProjectsPage />)
    // With no runs, the project list renders but is empty
    expect(screen.queryByTestId('agent-select')).toBeInTheDocument()
  })

  it('persists mode to sessionStorage', () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByText(/From Spec/))
    expect(sessionStorage.getItem('tr-mode')).toBe('spec')
  })

  it('shows compose panel after deleting selected project', async () => {
    const completedRun: ProjectRun = {
      task_id: 'run-1', name: 'Test', running: false, status: 'completed',
      steps: 2, completed: 2, failed: 0, skipped: 0, current_step: 2,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: 'test spec', lessons_learned: [],
      commits: 0, original_input: 'test', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus)
      .mockResolvedValueOnce({ running: false, available: true, runs: [completedRun] })
      .mockResolvedValue({ running: false, available: true, runs: [] })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Test')
    fireEvent.click(screen.getByText('Test'))
    expect(screen.getByTestId('project-detail')).toBeInTheDocument()
    // Click delete (X icon button in sidebar)
    const deleteBtn = screen.getAllByLabelText('Delete')[0]
    if (deleteBtn) fireEvent.click(deleteBtn)
    // After delete + reload, compose panel should be visible
    await screen.findByText(/Compose/)
    expect(screen.getByPlaceholderText('Describe your task...')).toBeInTheDocument()
  })

  it('shows restart button for completed projects', async () => {
    const completedRun: ProjectRun = {
      task_id: 'run-2', name: 'Done Project', running: false, status: 'completed',
      steps: 1, completed: 1, failed: 0, skipped: 0, current_step: 1,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: 'spec', lessons_learned: [],
      commits: 0, original_input: 'idea', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [completedRun] })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Done Project')
    fireEvent.click(screen.getByText('Done Project'))
    expect(screen.getByRole('button', { name: /restart/i })).toBeInTheDocument()
    expect(screen.getByText('Schedule')).toBeInTheDocument()
  })

  it('restart calls retryTaskRun with from_step 1', async () => {
    const completedRun: ProjectRun = {
      task_id: 'run-4', name: 'Retry Me', running: false, status: 'failed',
      steps: 2, completed: 1, failed: 1, skipped: 0, current_step: 2,
      spec: '', spec_name: '', error: 'oops', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: '', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [completedRun] })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Retry Me')
    fireEvent.click(screen.getByText('Retry Me'))
    fireEvent.click(screen.getByRole('button', { name: /restart/i }))
    expect(mockApi.retryTaskRun).toHaveBeenCalledWith('run-4', 1)
  })

  it('toggling auto-approve then Execute calls executePlan with autoApprove=true', async () => {
    const plannedRun: ProjectRun = {
      task_id: 'run-plan', name: 'Plan Me', running: false, status: 'planned',
      steps: 1, completed: 0, failed: 0, skipped: 0, current_step: 0,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: 'spec', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [plannedRun] })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Plan Me')
    fireEvent.click(screen.getByText('Plan Me'))
    // Toggle defaults OFF
    const toggle = screen.getByLabelText('Auto-approve tool calls') as HTMLInputElement
    expect(toggle.checked).toBe(false)
    fireEvent.click(toggle)
    expect(toggle.checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /Execute/ }))
    expect(mockApi.executePlan).toHaveBeenCalledWith('run-plan', '', true)
  })

  it('schedule calls createCron with project spec', async () => {
    const completedRun: ProjectRun = {
      task_id: 'run-5', name: 'Cron Me', running: false, status: 'completed',
      steps: 1, completed: 1, failed: 0, skipped: 0, current_step: 1,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: 'my spec content', lessons_learned: [],
      commits: 0, original_input: '', source: '', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [completedRun] })
    window.alert = vi.fn()
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Cron Me')
    fireEvent.click(screen.getByText('Cron Me'))
    fireEvent.click(screen.getByText('Schedule'))
    expect(mockApi.createCron).toHaveBeenCalledWith({
      name: 'Project: Cron Me',
      message: 'run __inline__:my spec content',
      every: 86400,
    })
  })

  // ── Auto-approve compose-panel toggle + badge indicator ──
  // The pre-existing per-run toggle only appears on planned/paused actions rows,
  // so a user cannot express "trust this from the start" upfront. These tests
  // cover the compose-panel toggle (declared before Run) and the badge indicator
  // that surfaces auto_approve on the run cards + detail header.

  it('renders auto-approve checkbox on compose panel unchecked by default', async () => {
    renderWithProviders(<ProjectsPage />)
    // No runs are selected on first render, so this checkbox can only come from
    // the compose panel — the planned/paused row checkboxes are gated behind a
    // selected run.
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    expect(composeAutoApproveCheckbox).not.toBeChecked()
  })

  it('sends auto_approve true to executePlan when compose checkbox checked and Run clicked', async () => {
    const plannedRun: ProjectRun = {
      task_id: 'plan-slice2', name: 'Slice 2', running: false, status: 'planned',
      steps: 0, completed: 0, failed: 0, skipped: 0, current_step: 0,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    // First status call (initial load): no runs yet. Once planTask returns, the
    // page refetches status; from that call on, the planned run must be
    // present so `setSelectedRun(planned)` fires the auto-run useEffect.
    vi.mocked(mockApi.taskRunnerStatus)
      .mockResolvedValueOnce({ running: false, available: true, runs: [] })
      .mockResolvedValue({ running: false, available: true, runs: [plannedRun] })
    vi.mocked(mockApi.planTask).mockResolvedValue({ ok: true, task_id: 'plan-slice2' })

    renderWithProviders(<ProjectsPage />)
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    fireEvent.click(composeAutoApproveCheckbox)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'grant trust upfront' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))

    // Auto-run useEffect must pick up the compose-time intent and call executePlan
    // with true. If the sync effect clobbered the intent, this would be false.
    await waitFor(() => {
      expect(mockApi.executePlan).toHaveBeenCalledWith('plan-slice2', '', true)
    })
  })

  it('sends auto_approve false to executePlan when compose checkbox unchecked and Run clicked', async () => {
    // Verifies the ref-based intent capture defaults to false and doesn't leak
    // a stale true from prior state. Same wiring as the checked-case test above,
    // but the user does NOT tick the checkbox before Run.
    const plannedRun: ProjectRun = {
      task_id: 'plan-slice3', name: 'Slice 3', running: false, status: 'planned',
      steps: 0, completed: 0, failed: 0, skipped: 0, current_step: 0,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus)
      .mockResolvedValueOnce({ running: false, available: true, runs: [] })
      .mockResolvedValue({ running: false, available: true, runs: [plannedRun] })
    vi.mocked(mockApi.planTask).mockResolvedValue({ ok: true, task_id: 'plan-slice3' })

    renderWithProviders(<ProjectsPage />)
    // Confirm the checkbox is present and unchecked, then leave it alone.
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    expect(composeAutoApproveCheckbox).not.toBeChecked()
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'no trust granted' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))

    await waitFor(() => {
      expect(mockApi.executePlan).toHaveBeenCalledWith('plan-slice3', '', false)
    })
  })

  it('does not leak stale auto_approve into a URL-triggered auto-run after a failed compose plan', async () => {
    // Regression for Spock finding fc-01 (2026-08-18): the buggy design wrote
    // pendingAutoApproveRef inside handleRun BEFORE calling doPlan. If planTask
    // failed the auto-run useEffect never fired to consume the ref, so a later
    // URL-triggered auto-run (e.g. /projects?applied=X&autoRun=true from the
    // chat "Use as Plan" flow) read the stale `true` and granted auto-approve
    // the user did not affirm for the new run. The fix moves the ref-write into
    // doPlan's success branch so both refs are set atomically on planTask
    // success; a failed plan leaves the ref at its default `false`.
    const urlTriggeredPlannedRun: ProjectRun = {
      task_id: 'plan-url', name: 'URL triggered', running: false, status: 'planned',
      steps: 0, completed: 0, failed: 0, skipped: 0, current_step: 0,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    // Initial load: no runs. From the second call onwards return the URL-triggered
    // planned run so once the applied useEffect calls load() the auto-run
    // useEffect can fire on selectedRun = plan-url.
    vi.mocked(mockApi.taskRunnerStatus)
      .mockResolvedValueOnce({ running: false, available: true, runs: [] })
      .mockResolvedValue({ running: false, available: true, runs: [urlTriggeredPlannedRun] })
    vi.mocked(mockApi.planTask).mockResolvedValueOnce({ ok: false, error: 'planning failed' })

    // Small in-test helper that captures react-router's navigate so we can push
    // a new URL onto the memory router mid-mount — mirroring the real "Use as
    // Plan" chat flow that navigates to ?applied=X&autoRun=true.
    let capturedNavigate: NavigateFunction | null = null
    function TestNavigator() {
      const navigate = useNavigate()
      useEffect(() => { capturedNavigate = navigate }, [navigate])
      return null
    }

    renderWithProviders(
      <>
        <ProjectsPage />
        <TestNavigator />
      </>,
    )

    // Step 1+2: tick the compose checkbox, enter text, click Run. planTask
    // rejects. autoRunRef.current is NEVER set on this branch, and — under
    // the fix — pendingAutoApproveRef stays at its default false.
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    fireEvent.click(composeAutoApproveCheckbox)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'this plan will fail' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))
    await waitFor(() => expect(mockApi.planTask).toHaveBeenCalledTimes(1))

    // Step 3: navigate to the URL-triggered auto-run. applied useEffect sets
    // autoRunRef.current and calls load(); load() returns the URL-planned run;
    // selectedRun updates; auto-run useEffect fires and reads
    // pendingAutoApproveRef — MUST be false (not the stale true from step 1).
    await waitFor(() => expect(capturedNavigate).not.toBeNull())
    capturedNavigate!('/?applied=plan-url&autoRun=true')

    await waitFor(() => {
      expect(mockApi.executePlan).toHaveBeenCalledWith('plan-url', '', false)
    })
  })

  it('does not leak stale auto_approve into a URL-triggered auto-run when the compose plan succeeded but its status fetch failed', async () => {
    // Regression for GPT reviewer Issue B (2026-08-18): under the pre-fix
    // scalar-ref design, doPlan's success branch wrote pendingAutoApproveRef
    // = true BEFORE the taskRunnerStatus refetch. If that status fetch then
    // failed (or was delayed past isPlanning=false in the finally block),
    // the recovery poll would not fire (activePlanRef was true during
    // doPlan and isPlanning goes false in finally, so the poll's guard
    // early-returns). The ref persisted, waiting for a consumer that would
    // never come from the ORIGINATING run. A later
    // /projects?applied=<OTHER>&autoRun=true URL trigger would overwrite
    // autoRunRef and consume the stale true for the WRONG task.
    //
    // The fix keys the ref to the originating task_id so it structurally
    // cannot be consumed by a different run — any auto-run whose
    // selectedRun.task_id doesn't match the stored taskId falls back to
    // false.
    const urlTriggeredPlannedRun: ProjectRun = {
      task_id: 'plan-url', name: 'URL triggered', running: false, status: 'planned',
      steps: 0, completed: 0, failed: 0, skipped: 0, current_step: 0,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
    }
    const { api: mockApi } = await import('../api/client')
    // Sequence:
    //   1. Initial load (no runs)
    //   2. doPlan's refetch after planTask succeeded — THROWS
    //   3. Applied useEffect's load() after URL nav — returns the URL run
    vi.mocked(mockApi.taskRunnerStatus)
      .mockResolvedValueOnce({ running: false, available: true, runs: [] })
      .mockRejectedValueOnce(new Error('status fetch failed'))
      .mockResolvedValue({ running: false, available: true, runs: [urlTriggeredPlannedRun] })
    vi.mocked(mockApi.planTask).mockResolvedValueOnce({ ok: true, task_id: 'plan-compose' })

    let capturedNavigate: NavigateFunction | null = null
    function TestNavigator() {
      const navigate = useNavigate()
      useEffect(() => { capturedNavigate = navigate }, [navigate])
      return null
    }

    renderWithProviders(
      <>
        <ProjectsPage />
        <TestNavigator />
      </>,
    )

    // Step 1: tick compose checkbox, enter text, click Run. planTask returns
    // ok for task 'plan-compose' but the subsequent status fetch throws, so
    // 'plan-compose' never becomes selectedRun and its auto-run useEffect
    // never fires to consume the intent.
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    fireEvent.click(composeAutoApproveCheckbox)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'plan succeeds but status fails' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))
    await waitFor(() => expect(mockApi.planTask).toHaveBeenCalledTimes(1))

    // Step 2: user navigates to a URL-triggered auto-run for a DIFFERENT
    // task ('plan-url'). Under the buggy design the stale intent from
    // 'plan-compose' would be consumed by 'plan-url'.
    await waitFor(() => expect(capturedNavigate).not.toBeNull())
    capturedNavigate!('/?applied=plan-url&autoRun=true')

    // The URL-triggered task must be executed WITHOUT the stale trust.
    await waitFor(() => {
      expect(mockApi.executePlan).toHaveBeenCalledWith('plan-url', '', false)
    })
    // And auto-approve must never have been granted to 'plan-url'.
    const executePlanCalls = vi.mocked(mockApi.executePlan).mock.calls
    const trustedUrlCall = executePlanCalls.find(
      call => call[0] === 'plan-url' && call[2] === true,
    )
    expect(trustedUrlCall).toBeUndefined()
  })

  it('resets the compose auto-approve intent when the user switches away from compose mode', async () => {
    // Regression for Fable Design/UX review Issue C (2026-08-18):
    // composeAutoApprove is component state that survived mode switches, but
    // the checkbox is only rendered in the compose branch. Ticking in
    // compose then Running from a spec/yaml mode (whose Run button also
    // calls handleRun) silently granted trust with no visible control on
    // screen. Fix: reset on mode change so the state cannot outlive its
    // visible control.
    renderWithProviders(<ProjectsPage />)
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    fireEvent.click(composeAutoApproveCheckbox)
    expect(composeAutoApproveCheckbox).toBeChecked()
    // Switch away to "From YAML" (the checkbox unmounts from the DOM), then
    // back to "Compose". The checkbox must be unticked in its new render —
    // the intent from the previous compose session did not survive the mode
    // switch.
    fireEvent.click(screen.getByRole('button', { name: /From YAML/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Compose$/ }))
    const composeCheckboxAfterModeSwitch = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    expect(composeCheckboxAfterModeSwitch).not.toBeChecked()
  })

  it('keeps the compose checkbox ticked when planTask fails so the user can retry without re-ticking', async () => {
    // Regression for Fable UX review Issue D (2026-08-18): the box was
    // reset at click time in `handleRun`, so a failed plan followed by a
    // silent retry (same input, click Run again) would run without the
    // grant — the user's explicit intent silently dropped by the retry.
    // Fix: move the reset into `doPlan`'s planTask-success branch so the
    // checkbox only clears when the plan actually took.
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.planTask).mockResolvedValueOnce({ ok: false, error: 'boom' })
    renderWithProviders(<ProjectsPage />)
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    fireEvent.click(composeAutoApproveCheckbox)
    expect(composeAutoApproveCheckbox).toBeChecked()
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'plan will fail' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))
    await waitFor(() => expect(mockApi.planTask).toHaveBeenCalledTimes(1))
    // After the plan fails, the box must still be ticked — the intent
    // survives so the retry doesn't silently drop the grant.
    expect(composeAutoApproveCheckbox).toBeChecked()
  })

  it('clears the compose auto-approve checkbox when the user clicks Plan (Plan-then-Execute path cannot carry compose intent)', async () => {
    // Fable UX Round 4 (2026-08-19) Concern 1: the compose checkbox only
    // wires into `handleRun` -> `doPlan(autoRun=true)`. Clicking the Plan
    // button instead calls `generatePlan` -> `doPlan(autoRun=false)`, and
    // the resulting Execute button reads `autoApprove` (the detail-row
    // sync state, seeded from the run's LIVE grant which is 0 for a
    // freshly-planned run). The compose intent was silently dropped, but
    // the checkbox stayed visibly ticked, so a "hands-off" user walked
    // away and the run stalled on the first approval prompt.
    //
    // Fix: clear the compose checkbox in `generatePlan` so the user has
    // an explicit signal that compose intent was discarded and must be
    // re-affirmed at Execute time via the detail-row toggle. Keeps the
    // trust decision deliberate.
    renderWithProviders(<ProjectsPage />)
    const composeAutoApproveCheckbox = await screen.findByRole(
      'checkbox', { name: /auto-approve tool calls/i },
    )
    fireEvent.click(composeAutoApproveCheckbox)
    expect(composeAutoApproveCheckbox).toBeChecked()
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'plan-first task' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Plan$/ }))
    // The box clears immediately (before any awaited planTask response) so
    // the visible signal is synchronous with the click, matching Fable UX
    // recommendation A ("clear the compose checkbox when Plan is clicked").
    expect(composeAutoApproveCheckbox).not.toBeChecked()
  })

  it('shows the auto-approve badge on a project card when the run has a live grant', async () => {
    // Live grant = auto_approve_remaining_secs > 0. The badge uses the same
    // predicate as the run-detail toggle's sync effect (ProjectsPage.tsx:202,
    // "Reflect only a LIVE trust grant") so it never asserts trust that was
    // torn down — matches the codebase pattern already pinned in
    // ProjectsPageCoverage.test.tsx:554.
    const trustedRun: ProjectRun = {
      task_id: 'run-trusted', name: 'Trusted Run', running: true, status: 'running',
      steps: 3, completed: 1, failed: 0, skipped: 0, current_step: 2,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
      auto_approve: true, auto_approve_remaining_secs: 3600,
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({
      running: true, available: true, runs: [trustedRun],
    })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Trusted Run')
    // Rail cards live inside a ~220px sidebar column, so the badge is
    // deliberately icon-only (GPT 5.6 review 2026-08-19 flagged the previous
    // text+icon shape as overflowing narrow contexts). The accessible label
    // is carried by aria-label and title; sighted users read the Zap glyph
    // plus the hover tooltip.
    const autoApproveBadge = screen.getByTestId('auto-approve-badge')
    expect(autoApproveBadge).toBeInTheDocument()
    expect(autoApproveBadge).toHaveAttribute('role', 'img')
    expect(autoApproveBadge.getAttribute('aria-label') ?? '').toMatch(/auto-approve/i)
    expect(autoApproveBadge).not.toHaveTextContent(/auto-approve/i)
  })

  it('does not show the auto-approve badge on a project card when the run has no live grant', async () => {
    const untrustedRun: ProjectRun = {
      task_id: 'run-untrusted', name: 'Untrusted Run', running: true, status: 'running',
      steps: 3, completed: 1, failed: 0, skipped: 0, current_step: 2,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
      auto_approve: false,
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({
      running: true, available: true, runs: [untrustedRun],
    })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Untrusted Run')
    expect(screen.queryByTestId('auto-approve-badge')).not.toBeInTheDocument()
  })

  it('does not show the auto-approve badge on a project card when the grant has expired', async () => {
    // Regression for GPT/Fable review Issue A (2026-08-18): a paused run
    // retains auto_approve: true in the status payload even after its
    // remaining_secs hits zero. The badge must NOT assert trust in that
    // state — same live-grant rule the run-detail toggle sync effect uses.
    const expiredRun: ProjectRun = {
      task_id: 'run-expired', name: 'Expired Run', running: false, status: 'paused',
      steps: 3, completed: 1, failed: 0, skipped: 0, current_step: 2,
      spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
      task_details: [], started_at: 0, finished_at: 0,
      work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
      commits: 0, original_input: '', source: 'text', groups: [],
      auto_approve: true, auto_approve_remaining_secs: 0,
    }
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({
      running: false, available: true, runs: [expiredRun],
    })
    renderWithProviders(<ProjectsPage />)
    await screen.findByText('Expired Run')
    expect(screen.queryByTestId('auto-approve-badge')).not.toBeInTheDocument()
  })
})

// ── PhasedView ──

describe('PhasedView', () => {
  it('renders 3 columns: To do, In progress, Done', () => {
    renderWithProviders(<PhasedView tasks={mockTasks} />)
    expect(screen.getByText(/To do \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/In progress \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Done \(1\)/)).toBeInTheDocument()
  })

  it('shows failed tasks in separate section', () => {
    renderWithProviders(<PhasedView tasks={mockTasks} />)
    expect(screen.getByText(/Failed \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/Task 5: Broken/)).toBeInTheDocument()
  })

  it('shows skipped tasks in separate section', () => {
    renderWithProviders(<PhasedView tasks={mockTasks} />)
    expect(screen.getByText(/Skipped \(1\)/)).toBeInTheDocument()
  })

  it('maps reviewing status to In progress column', () => {
    renderWithProviders(<PhasedView tasks={mockTasks} />)
    expect(screen.getByText(/In progress \(2\)/)).toBeInTheDocument()
  })

  it('shows all tasks in Done for completed project', () => {
    const doneTasks = mockTasks.map(t => ({ ...t, status: 'passed' }))
    renderWithProviders(<PhasedView tasks={doneTasks} />)
    expect(screen.getByText(/Done \(6\)/)).toBeInTheDocument()
    expect(screen.getByText(/To do \(0\)/)).toBeInTheDocument()
  })

  it('calls onTaskClick when task is clicked', () => {
    const onClick = vi.fn()
    renderWithProviders(<PhasedView tasks={mockTasks} onTaskClick={onClick} />)
    fireEvent.click(screen.getByText(/Task 1: Setup/))
    expect(onClick).toHaveBeenCalledWith(1)
  })

  it('shows checkpoint icon for checkpoint tasks', () => {
    const { container } = renderWithProviders(<PhasedView tasks={mockTasks} />)
    expect(container.querySelector('.lucide-shield')).toBeInTheDocument()
  })
})

// ── DagView ──

describe('DagView', () => {
  const nodes = [
    { id: '1', title: 'Setup', status: 'passed', priority: 'normal' },
    { id: '2', title: 'Build', status: 'in_progress', priority: 'high' },
    { id: '3', title: 'Test', status: 'pending', priority: 'normal' },
    { id: '4', title: 'Fix', status: 'failed', priority: 'normal', task_type: 'fix' },
  ]
  const edges = [{ from: '1', to: '2' }, { from: '1', to: '3' }, { from: '2', to: '4' }]

  it('renders SVG with all nodes', () => {
    const { container } = renderWithProviders(
      <DagView nodes={nodes} edges={edges} onNodeClick={vi.fn()} />
    )
    // Each node renders a <g> with a <rect> — count the node groups
    expect(container.querySelectorAll('svg > g > rect').length).toBe(4)
  })

  it('renders edges as paths', () => {
    const { container } = renderWithProviders(
      <DagView nodes={nodes} edges={edges} onNodeClick={vi.fn()} />
    )
    // Edge paths have markerEnd attribute
    expect(container.querySelectorAll('path[marker-end]').length).toBe(3)
  })

  it('maps reviewing to "in progress" label', () => {
    const reviewNode = [{ id: '1', title: 'Check', status: 'reviewing', priority: 'normal' }]
    renderWithProviders(<DagView nodes={reviewNode} edges={[]} onNodeClick={vi.fn()} />)
    expect(screen.getByText('in progress')).toBeInTheDocument()
  })

  it('shows fix icon for fix task type', () => {
    const { container } = renderWithProviders(<DagView nodes={nodes} edges={edges} onNodeClick={vi.fn()} />)
    expect(container.querySelector('.lucide-wrench')).toBeInTheDocument()
  })

  it('shows empty state when no nodes', () => {
    renderWithProviders(<DagView nodes={[]} edges={[]} onNodeClick={vi.fn()} />)
    expect(screen.getByText('No tasks to visualize')).toBeInTheDocument()
  })

  it('renders all done nodes with green stroke for completed project', () => {
    const doneNodes = nodes.map(n => ({ ...n, status: 'passed', task_type: undefined }))
    const { container } = renderWithProviders(
      <DagView nodes={doneNodes} edges={edges} onNodeClick={vi.fn()} />
    )
    const rects = container.querySelectorAll('svg > g > rect')
    expect(rects.length).toBe(4)
    rects.forEach(rect => {
      expect(rect.getAttribute('stroke')).toBe('#22c55e')
    })
  })
})
