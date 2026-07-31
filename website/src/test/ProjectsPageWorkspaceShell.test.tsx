// Task Runner workspace shell — the parts that were brought in line with the
// other builtin apps (Issue Radar as the reference):
//
//   * the page routes through BUILTIN_COMPONENT_REGISTRY rather than a hardcoded
//     <Route> in App.tsx,
//   * the run rail is a real resizable/collapsible column, present in every state,
//   * the page is full-bleed (no dashboard PageHeader, no page gutters),
//   * no loading state spins.
//
// Each test below fails against the pre-alignment page, so the suite is a
// regression guard rather than a restatement of the implementation.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectsPage from '../pages/ProjectsPage'
import { BUILTIN_COMPONENT_REGISTRY, hasBuiltinComponent } from '../apps/builtinRegistry'
import {
  COLLAPSED_RAIL_WIDTH, DEFAULT_RAIL_WIDTH, MAX_RAIL_WIDTH, MIN_RAIL_WIDTH,
  RAIL_COLLAPSED_KEY, RAIL_WIDTH_KEY, loadRailCollapsed, loadRailWidth,
} from '../pages/projectsLayout'
import type { ProjectRun } from '../types'

vi.mock('../pages/ProjectDetailPage', () => ({ default: () => <div data-testid="project-detail">Detail</div> }))
vi.mock('../components/AgentSelector', () => ({ default: () => <select data-testid="agent-select" /> }))

vi.mock('../api/client', () => ({
  api: {
    taskRunnerStatus: vi.fn().mockResolvedValue({ running: false, available: true, runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    refineStatus: vi.fn().mockResolvedValue({ status: 'idle', text: '', error: '' }),
    cancelTaskRunner: vi.fn().mockResolvedValue({ ok: true }),
    deleteTaskRun: vi.fn().mockResolvedValue({ ok: true }),
    planTask: vi.fn().mockResolvedValue({ ok: true, task_id: 'plan-1' }),
    cancelPlan: vi.fn().mockResolvedValue({ ok: true }),
    executePlan: vi.fn().mockResolvedValue({ ok: true }),
    refineTaskInput: vi.fn().mockResolvedValue({ ok: true }),
    refineCancel: vi.fn().mockResolvedValue({ ok: true }),
    createCron: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const run: ProjectRun = {
  task_id: 'run-1', name: 'Existing', running: false, status: 'completed',
  steps: 2, completed: 2, failed: 0, skipped: 0, current_step: 2,
  spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
  task_details: [], started_at: 0, finished_at: 0,
  work_dir: '', branch_name: '', spec_content: 'spec', lessons_learned: [],
  commits: 0, original_input: '', source: 'text', groups: [],
}

/** The overshoot the resize hook requires before it snaps (its DEFAULT_SLOP). */
const SLOP = 48

function drag(handle: HTMLElement, dx: number, id = 1) {
  fireEvent.pointerDown(handle, { clientX: 0, pointerId: id })
  fireEvent.pointerMove(handle, { clientX: dx, pointerId: id })
  fireEvent.pointerUp(handle, { clientX: dx, pointerId: id })
}

const railHandle = () => screen.getByRole('separator', { name: 'Resize sidebar' })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
})

describe('Task Runner — builtin app routing', () => {
  it('resolves /projects through the builtin component registry', () => {
    // Every other builtin page is looked up here by its manifest route; a
    // hardcoded App.tsx <Route> would leave this false and skip the shared
    // Suspense + ErrorBoundary wrapper the registry route provides.
    expect(hasBuiltinComponent('/projects')).toBe(true)
    expect(BUILTIN_COMPONENT_REGISTRY['/projects']).toBeTruthy()
  })
})

describe('Task Runner — workspace shell', () => {
  it('is full-bleed: no dashboard PageHeader', () => {
    // The other builtin app pages own their whole viewport rather than sitting
    // under the generic title/subtitle block.
    renderWithProviders(<ProjectsPage />)
    expect(screen.queryByTestId('page-header')).not.toBeInTheDocument()
  })

  it('still names the app, in the rail instead of a page title', () => {
    renderWithProviders(<ProjectsPage />)
    expect(screen.getByText('Task Runner')).toBeInTheDocument()
  })

  it('shows the rail before any run exists', async () => {
    // Previously the rail only appeared once runs existed, so the main column
    // jumped sideways the moment the first run landed.
    renderWithProviders(<ProjectsPage />)
    expect(await screen.findByText('No runs yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New Task/ })).toBeInTheDocument()
  })

  it('exposes the rail edge as an accessible vertical separator', () => {
    renderWithProviders(<ProjectsPage />)
    const handle = railHandle()
    expect(handle.getAttribute('aria-orientation')).toBe('vertical')
  })
})

describe('Task Runner — rail resize and collapse', () => {
  it('widens the rail on drag and persists the new width', () => {
    renderWithProviders(<ProjectsPage />)
    drag(railHandle(), 60)
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH + 60)
  })

  it('clamps the rail to its maximum', () => {
    renderWithProviders(<ProjectsPage />)
    drag(railHandle(), 5000)
    expect(loadRailWidth()).toBe(MAX_RAIL_WIDTH)
  })

  it('collapses to the icon strip when dragged well past the minimum', async () => {
    renderWithProviders(<ProjectsPage />)
    // Past the minimum AND past the slop, so the snap fires rather than the
    // width just stopping at MIN_RAIL_WIDTH.
    drag(railHandle(), -(DEFAULT_RAIL_WIDTH - MIN_RAIL_WIDTH) - SLOP - 1)
    expect(loadRailCollapsed()).toBe(true)
    expect(await screen.findByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()
  })

  it('does not collapse when the drag merely reaches the minimum', () => {
    renderWithProviders(<ProjectsPage />)
    drag(railHandle(), -(DEFAULT_RAIL_WIDTH - MIN_RAIL_WIDTH))
    expect(loadRailCollapsed()).toBe(false)
    expect(loadRailWidth()).toBe(MIN_RAIL_WIDTH)
  })

  it('restores a collapsed rail from localStorage, at the stored open width', async () => {
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    renderWithProviders(<ProjectsPage />)
    const expand = await screen.findByRole('button', { name: 'Expand sidebar' })
    // Collapsed: the strip is showing, and the run list is not.
    expect(screen.queryByRole('button', { name: /New Task/ })).not.toBeInTheDocument()
    fireEvent.click(expand)
    // Reopening returns the width the user had chosen, not the default.
    expect(loadRailWidth()).toBe(400)
    expect(await screen.findByRole('button', { name: /New Task/ })).toBeInTheDocument()
  })

  it('keeps the collapsed strip narrow', () => {
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    renderWithProviders(<ProjectsPage />)
    const strip = screen.getByRole('button', { name: 'Expand sidebar' }).closest('aside')
    expect(strip).toHaveStyle({ width: `${COLLAPSED_RAIL_WIDTH}px` })
  })
})

describe('Task Runner — rail is keyboard operable', () => {
  it('exposes the handle as a focusable splitter reporting its position', () => {
    renderWithProviders(<ProjectsPage />)
    const handle = railHandle()
    expect(handle).toHaveAttribute('tabindex', '0')
    expect(handle).toHaveAttribute('aria-valuenow', String(DEFAULT_RAIL_WIDTH))
    expect(handle).toHaveAttribute('aria-valuemin', String(MIN_RAIL_WIDTH))
    expect(handle).toHaveAttribute('aria-valuemax', String(MAX_RAIL_WIDTH))
  })

  it('resizes on arrow keys, with a coarser Shift step', () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.keyDown(railHandle(), { key: 'ArrowRight' })
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH + 16)
    fireEvent.keyDown(railHandle(), { key: 'ArrowRight', shiftKey: true })
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH + 16 + 64)
    fireEvent.keyDown(railHandle(), { key: 'ArrowLeft' })
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH + 64)
  })

  it('leaves vertical arrows alone so the columns either side still scroll', () => {
    renderWithProviders(<ProjectsPage />)
    const before = loadRailWidth()
    fireEvent.keyDown(railHandle(), { key: 'ArrowDown' })
    fireEvent.keyDown(railHandle(), { key: 'ArrowUp' })
    expect(loadRailWidth()).toBe(before)
  })

  it('collapses on a left step past the minimum, and reopens on a right step', async () => {
    renderWithProviders(<ProjectsPage />)
    // Walk in to the minimum, then one more step tips it over.
    for (let i = 0; i < 20; i++) fireEvent.keyDown(railHandle(), { key: 'ArrowLeft', shiftKey: true })
    expect(loadRailCollapsed()).toBe(true)
    expect(await screen.findByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()
    // A collapsed rail must be reachable from the keyboard without the mouse.
    fireEvent.keyDown(railHandle(), { key: 'ArrowRight' })
    expect(loadRailCollapsed()).toBe(false)
    expect(await screen.findByRole('button', { name: /New Task/ })).toBeInTheDocument()
  })

  it('gives the collapsed strip a visible focus ring', () => {
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    renderWithProviders(<ProjectsPage />)
    // It is the only keyboard route back to the expanded rail, so a Tab user
    // must be able to see it take focus.
    const expand = screen.getByRole('button', { name: 'Expand sidebar' })
    expect(expand.className).toContain('focus-ring')
    expect(expand.className).not.toContain('outline-none')
  })
})

describe('Task Runner — loading states do not spin', () => {  it('renders the planning banner without a rotating spinner', async () => {
    const { api: mockApi } = await import('../api/client')
    // Park planTask so the banner stays mounted while we inspect it.
    vi.mocked(mockApi.planTask).mockImplementation(() => new Promise(() => {}))
    const { container } = renderWithProviders(<ProjectsPage />)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), {
      target: { value: 'ship the thing' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Plan/ }))
    expect(await screen.findByText(/Generating execution plan/)).toBeInTheDocument()
    expect(container.querySelector('.animate-spin')).toBeNull()
  })

  it('shows the refining state without a pulsing label', async () => {
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.refineStatus).mockResolvedValue({ status: 'running', text: '', error: '' })
    const { container } = renderWithProviders(<ProjectsPage />)
    expect(await screen.findByText(/Refining/)).toBeInTheDocument()
    expect(container.querySelector('.animate-spin')).toBeNull()
    expect(container.querySelector('.animate-pulse')).toBeNull()
    // The motion moved into a shimmer placeholder instead.
    expect(container.querySelector('.skeleton')).not.toBeNull()
  })
})

describe('Task Runner — run rail contents', () => {
  it('lists runs and opens one in the main column', async () => {
    const { api: mockApi } = await import('../api/client')
    vi.mocked(mockApi.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [run] })
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Existing/ }))
    expect(await screen.findByTestId('project-detail')).toBeInTheDocument()
    // The rail survives opening a run — it is a column, not a drawer.
    expect(railHandle()).toBeInTheDocument()
  })
})
