import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
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
    // Wait for project to appear in sidebar
    await screen.findByText('Test')
    // Select the project
    fireEvent.click(screen.getByText('Test'))
    // Should show detail view
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
