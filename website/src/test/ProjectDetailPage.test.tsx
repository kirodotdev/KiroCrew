import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectDetailPage from '../pages/ProjectDetailPage'
import { api } from '../api/client'
import type { ProjectRun } from '../types'

vi.mock('../pages/aidlc/DagView', () => ({ default: ({ nodes }: { nodes: unknown[] }) => <div data-testid="dag-view">{nodes.length} nodes</div> }))
vi.mock('../pages/aidlc/PhasedView', () => ({ default: ({ tasks }: { tasks: unknown[] }) => <div data-testid="phased-view">{tasks.length} tasks</div> }))
vi.mock('../pages/aidlc/TaskDetailPanel', () => ({ default: () => <div data-testid="task-panel">Panel</div> }))

const mockRun = (overrides: Partial<ProjectRun> = {}): ProjectRun => ({
  task_id: 'run-1', name: 'Test Run', running: false, status: 'completed',
  steps: 3, completed: 3, failed: 0, skipped: 0, current_step: 3,
  spec: 'test.md', spec_name: 'Test', error: '',
  tokens_used: 1000, replan_count: 0,
  started_at: Date.now() / 1000 - 60, finished_at: Date.now() / 1000,
  work_dir: '/tmp/test', branch_name: 'main', spec_content: '# Test spec',
  lessons_learned: [], commits: 1, original_input: 'test input', source: 'text',
  groups: [[1, 2], [3]],
  task_details: [
    { index: 1, title: 'Setup', description: 'Init', status: 'passed', error: '', result: 'done', attempts: 1, depends_on: [], requires_approval: false },
    { index: 2, title: 'Build', description: 'Compile', status: 'passed', error: '', result: 'ok', attempts: 1, depends_on: [], requires_approval: false },
    { index: 3, title: 'Test', description: 'Verify', status: 'passed', error: '', result: 'pass', attempts: 1, depends_on: [1, 2], requires_approval: false },
  ],
  ...overrides,
})

describe('ProjectDetailPage', () => {
  it('renders Idea and Tasks tabs', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    expect(screen.getByText('Idea')).toBeInTheDocument()
    expect(screen.getByText('Tasks')).toBeInTheDocument()
  })

  it('defaults to Tasks tab with DAG view', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    expect(screen.getByText('DAG')).toBeInTheDocument()
    expect(screen.getByText('Phased')).toBeInTheDocument()
    expect(screen.getByTestId('dag-view')).toBeInTheDocument()
  })

  it('switches to Idea tab showing spec content', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByText('Idea'))
    expect(screen.getByText('# Test spec')).toBeInTheDocument()
    expect(screen.getByText('Edit in Chat')).toBeInTheDocument()
  })

  it('shows "No spec content" when spec is empty', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ spec_content: '', original_input: '' })} />)
    fireEvent.click(screen.getByText('Idea'))
    expect(screen.getByText('No idea or spec content available.')).toBeInTheDocument()
  })

  it('switches to Phased view', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByText('Phased'))
    expect(screen.getByTestId('phased-view')).toBeInTheDocument()
  })

  it('hides DAG/Phased toggle on Idea tab', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByText('Idea'))
    expect(screen.queryByText('DAG')).not.toBeInTheDocument()
  })

  it('shows spec content when empty input falls back to original_input', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ spec_content: '', original_input: 'my idea' })} />)
    fireEvent.click(screen.getByText('Idea'))
    expect(screen.getByText('my idea')).toBeInTheDocument()
  })

  it('renders Export YAML button and calls exportPlanYaml on click', async () => {
    const spy = vi.spyOn(api, 'exportPlanYaml').mockResolvedValue(undefined)
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    const btn = screen.getByText('Export YAML')
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn)
    await waitFor(() => expect(spy).toHaveBeenCalledWith('run-1'))
    spy.mockRestore()
  })

  it('hides Export YAML button when the run has no tasks', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ task_details: [] })} />)
    expect(screen.queryByText('Export YAML')).not.toBeInTheDocument()
  })
})
