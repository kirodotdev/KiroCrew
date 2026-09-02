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
  it('renders the planning overlay without a rotating spinner', () => {
    // Loading states use a static glyph plus a shimmer placeholder; nothing spins.
    const { container } = renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'planning' })} />)
    expect(screen.getByText(/Generating execution plan/)).toBeInTheDocument()
    expect(container.querySelector('.animate-spin')).toBeNull()
    expect(container.querySelector('.skeleton')).not.toBeNull()
  })

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

  it('shows the auto-approve badge in the detail header when the run has a live grant', () => {
    // Mirror of the project-card badge in ProjectsPage. Live-grant predicate
    // matches the run-detail toggle sync at ProjectsPage.tsx:202. The detail
    // header has more room than the rail card so the visible "Auto-approve"
    // text is kept above the `sm` viewport breakpoint (per Fable UX
    // 2026-08-18); below `sm` it collapses to icon-only so a narrow phone
    // or heavily localized locale cannot overflow the row (per GPT 5.6
    // 2026-08-19). The text span is in the DOM either way, gated by CSS —
    // jsdom does not compute media queries, so `toHaveTextContent` finds it.
    renderWithProviders(<ProjectDetailPage run={mockRun({ auto_approve: true, auto_approve_remaining_secs: 3600 })} />)
    const autoApproveBadge = screen.getByTestId('auto-approve-badge')
    expect(autoApproveBadge).toBeInTheDocument()
    expect(autoApproveBadge).toHaveAttribute('role', 'img')
    expect(autoApproveBadge).toHaveTextContent(/auto-approve/i)
  })

  it('collapses the detail-header badge text to icon-only below the sm viewport breakpoint', () => {
    // Verify the responsive-hide contract: the text is wrapped in a span
    // with `hidden sm:inline` so overflow at 320px / 220px is closed while
    // wide viewports keep the human-readable label.
    renderWithProviders(<ProjectDetailPage run={mockRun({ auto_approve: true, auto_approve_remaining_secs: 3600 })} />)
    const autoApproveBadge = screen.getByTestId('auto-approve-badge')
    const textSpan = Array.from(autoApproveBadge.querySelectorAll('span')).find(
      element => /auto-approve/i.test(element.textContent ?? ''),
    )
    expect(textSpan, 'expected a child <span> with the visible label').toBeTruthy()
    expect(textSpan!.className).toMatch(/\bhidden\b/)
    expect(textSpan!.className).toMatch(/\bsm:inline\b/)
  })

  it('does not show the auto-approve badge in the detail header when the run has no live grant', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ auto_approve: false })} />)
    expect(screen.queryByTestId('auto-approve-badge')).not.toBeInTheDocument()
  })

  it('does not show the auto-approve badge in the detail header when the grant has expired', () => {
    // Regression for GPT/Fable review Issue A (2026-08-18): expired grant
    // (auto_approve: true, auto_approve_remaining_secs: 0) must NOT surface
    // trust on the detail header — matches the live-grant sync at
    // ProjectsPage.tsx:202.
    renderWithProviders(<ProjectDetailPage run={mockRun({ auto_approve: true, auto_approve_remaining_secs: 0 })} />)
    expect(screen.queryByTestId('auto-approve-badge')).not.toBeInTheDocument()
  })
})
