// Task Runner page — the paths the two existing ProjectsPage suites leave cold:
// spec/YAML file uploads, the refine round trip, the sessionStorage planning
// recovery poll, the ?applied / ?autoRun deep link, and every per-status button
// in the selected-run header (planned / planning / running / paused / completed).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectsPage from '../pages/ProjectsPage'
import { api } from '../api/client'
import type { ProjectRun, RunStatus } from '../types'

// The detail pane is exercised by its own suite; here it only has to prove the
// two callbacks ProjectsPage hands it are wired to the run under selection.
vi.mock('../pages/ProjectDetailPage', () => ({
  default: ({ onRetry, onRefresh }: { onRetry: (idx: number) => Promise<void>; onRefresh: () => void }) => (
    <div data-testid="project-detail">
      <button onClick={() => { void onRetry(3) }}>detail retry step</button>
      <button onClick={() => { onRefresh() }}>detail refresh</button>
    </div>
  ),
}))

vi.mock('../components/AgentSelector', () => ({
  default: ({ value, onChange }: { value: string; onChange: (name: string) => void }) => (
    <select
      aria-label="Agent picker"
      value={value}
      onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value)}
    >
      <option value="">default</option>
      <option value="reviewer">reviewer</option>
    </select>
  ),
}))

vi.mock('../api/client', () => ({
  api: {
    taskRunnerStatus: vi.fn(),
    kirocrewAgents: vi.fn(),
    planTask: vi.fn(),
    cancelPlan: vi.fn(),
    executePlan: vi.fn(),
    planContext: vi.fn(),
    deleteTaskRun: vi.fn(),
    cancelTaskRunner: vi.fn(),
    retryTaskRun: vi.fn(),
    renameTaskRun: vi.fn(),
    pauseTaskRun: vi.fn(),
    taskRunToChat: vi.fn(),
    refineStatus: vi.fn(),
    refineTaskInput: vi.fn(),
    refineCancel: vi.fn(),
    createCron: vi.fn(),
  },
}))

const mkRun = (overrides: Partial<ProjectRun> = {}): ProjectRun => ({
  task_id: 'run-1', name: 'Existing', running: false, status: 'completed' as RunStatus,
  steps: 2, completed: 1, failed: 0, skipped: 0, current_step: 1,
  spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
  task_details: [], started_at: 0, finished_at: 0,
  work_dir: '', branch_name: '', spec_content: 'spec body', lessons_learned: [],
  commits: 0, original_input: '', source: 'text', groups: [],
  ...overrides,
})

/** Every request the page can make, resolved with a benign default. Set
 *  unconditionally so no test inherits a previous test's mockResolvedValue. */
function resetApi(runs: ProjectRun[] = []) {
  vi.mocked(api.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs })
  vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [], default_agent: '' })
  vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: '', error: '' })
  vi.mocked(api.planTask).mockResolvedValue({ ok: true, task_id: 'plan-1' })
  vi.mocked(api.cancelPlan).mockResolvedValue({ ok: true })
  vi.mocked(api.executePlan).mockResolvedValue({ ok: true })
  vi.mocked(api.planContext).mockResolvedValue({ ok: true, context: 'PLAN CONTEXT' })
  vi.mocked(api.deleteTaskRun).mockResolvedValue({ ok: true })
  vi.mocked(api.cancelTaskRunner).mockResolvedValue({ ok: true })
  vi.mocked(api.retryTaskRun).mockResolvedValue({ ok: true })
  vi.mocked(api.renameTaskRun).mockResolvedValue({ ok: true })
  vi.mocked(api.pauseTaskRun).mockResolvedValue({ ok: true })
  vi.mocked(api.taskRunToChat).mockResolvedValue({ slot: 2 })
  vi.mocked(api.refineTaskInput).mockResolvedValue({ ok: true })
  vi.mocked(api.refineCancel).mockResolvedValue({ ok: true })
  vi.mocked(api.createCron).mockResolvedValue({ ok: true })
}


/** Select a run from the rail and return the header action row it opens. */
async function openRun(name = 'Existing'): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: `Open project ${name}` }))
  await screen.findByTestId('project-detail')
  return screen.getByTestId('project-detail').parentElement!.parentElement!
    .querySelector<HTMLElement>('div.border-b')!
}

let alertSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
  resetApi()
  alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
})

afterEach(() => {
  vi.useRealTimers()
  alertSpy.mockRestore()
})

describe('ProjectsPage — mode tabs', () => {
  it('switches to the YAML panel and back to Compose, persisting the choice', async () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From YAML' }))
    expect(screen.getByPlaceholderText('Paste YAML workflow or upload a .yaml file...')).toBeInTheDocument()
    expect(screen.getByText(/YAML workflows bypass the LLM decomposer/)).toBeInTheDocument()
    await waitFor(() => expect(sessionStorage.getItem('tr-mode')).toBe('yaml'))

    fireEvent.click(screen.getByRole('button', { name: 'Compose' }))
    expect(screen.getByPlaceholderText('Describe your task...')).toBeInTheDocument()
    await waitFor(() => expect(sessionStorage.getItem('tr-mode')).toBe('compose'))
  })

  it('keeps typed spec text in sessionStorage', async () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From Spec' }))
    const ta = screen.getByPlaceholderText('Paste spec content or upload a file...')
    fireEvent.change(ta, { target: { value: 'step one' } })
    expect((ta as HTMLTextAreaElement).value).toBe('step one')
    await waitFor(() => expect(sessionStorage.getItem('tr-spec')).toBe('step one'))
  })
})

describe('ProjectsPage — spec and YAML uploads', () => {
  it('reads a picked spec file into the spec textarea and clears the picker', async () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From Spec' }))
    const picker = screen.getByLabelText('Upload a file') as HTMLInputElement
    fireEvent.change(picker, { target: { files: [new File(['# plan body'], 'plan.md', { type: 'text/markdown' })] } })
    const ta = screen.getByPlaceholderText('Paste spec content or upload a file...') as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toBe('# plan body'))
    expect(picker.value).toBe('')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('refuses a YAML file on the spec tab and leaves the spec text untouched', async () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From Spec' }))
    const picker = screen.getByLabelText('Upload a file') as HTMLInputElement
    fireEvent.change(picker, { target: { files: [new File(['a: 1'], 'flow.yaml', { type: 'text/yaml' })] } })
    expect(alertSpy).toHaveBeenCalledWith('YAML files should be uploaded via the "From YAML" tab.')
    const ta = screen.getByPlaceholderText('Paste spec content or upload a file...') as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toBe(''))
  })

  it('reads a picked .yml file into the YAML textarea', async () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From YAML' }))
    const picker = screen.getByLabelText('Upload a file') as HTMLInputElement
    fireEvent.change(picker, { target: { files: [new File(['steps: []'], 'flow.yml', { type: 'text/yaml' })] } })
    const ta = screen.getByPlaceholderText('Paste YAML workflow or upload a .yaml file...') as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toBe('steps: []'))
    expect(picker.value).toBe('')
  })

  it('refuses a non-YAML file on the YAML tab', () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From YAML' }))
    const picker = screen.getByLabelText('Upload a file') as HTMLInputElement
    fireEvent.change(picker, { target: { files: [new File(['hi'], 'notes.md', { type: 'text/markdown' })] } })
    expect(alertSpy).toHaveBeenCalledWith('Only .yaml/.yml files are accepted here. Use the "From Spec" tab for other formats.')
  })

  it('ignores a picker change that carries no file', () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'From Spec' }))
    fireEvent.change(screen.getByLabelText('Upload a file'), { target: { files: [] } })
    expect(alertSpy).not.toHaveBeenCalled()
  })
})

describe('ProjectsPage — refine round trip', () => {
  it('does not start a refine with an empty prompt', () => {
    renderWithProviders(<ProjectsPage />)
    expect(screen.getByRole('button', { name: 'Refine into Spec' })).toBeDisabled()
    expect(api.refineTaskInput).not.toHaveBeenCalled()
  })

  it('renders a returned refined spec and plans from it', async () => {
    vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: 'REFINED SPEC', error: '' })
    renderWithProviders(<ProjectsPage />)
    const refinedTa = await screen.findByLabelText('Refined spec') as HTMLTextAreaElement
    expect(refinedTa.value).toBe('REFINED SPEC')
    fireEvent.change(refinedTa, { target: { value: 'REFINED SPEC v2' } })

    fireEvent.click(screen.getByRole('button', { name: 'Plan from Spec' }))
    await waitFor(() => expect(api.planTask).toHaveBeenCalledWith('REFINED SPEC v2', 'spec', undefined, '', ''))
  })

  it('runs the refined spec through the second Run button', async () => {
    vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: 'REFINED SPEC', error: '' })
    renderWithProviders(<ProjectsPage />)
    await screen.findByLabelText('Refined spec')
    // One Run in the compose row, one in the refined-spec row.
    const runButtons = screen.getAllByRole('button', { name: 'Run' })
    expect(runButtons).toHaveLength(2)
    fireEvent.click(runButtons[1])
    await waitFor(() => expect(api.planTask).toHaveBeenCalledWith('REFINED SPEC', 'spec', '', '', ''))
  })

  it('discards the refined spec', async () => {
    vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: 'REFINED SPEC', error: '' })
    renderWithProviders(<ProjectsPage />)
    await screen.findByLabelText('Refined spec')
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(screen.queryByLabelText('Refined spec')).not.toBeInTheDocument())
    expect(api.planTask).not.toHaveBeenCalled()
  })

  it('surfaces a refine error alongside the refined text', async () => {
    vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: 'PARTIAL', error: 'model timed out' })
    renderWithProviders(<ProjectsPage />)
    expect(await screen.findByText(/model timed out/)).toBeInTheDocument()
  })

  it('restores a server-side prompt into an empty compose box', async () => {
    vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: '', error: '', input: 'restored idea' })
    renderWithProviders(<ProjectsPage />)
    const ta = screen.getByPlaceholderText('Describe your task...') as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toBe('restored idea'))
  })

  it('falls back to idle when the refine status request fails', async () => {
    vi.mocked(api.refineStatus).mockRejectedValue(new Error('refine endpoint down'))
    renderWithProviders(<ProjectsPage />)
    expect(await screen.findByRole('button', { name: 'Refine into Spec' })).toBeInTheDocument()
    expect(screen.queryByText('Refining…')).not.toBeInTheDocument()
  })
})

describe('ProjectsPage — planning lifecycle', () => {
  it('selects the run that planTask produced', async () => {
    const planned = mkRun({ task_id: 'plan-1', name: 'Planned Work', status: 'planned' })
    resetApi([planned])
    renderWithProviders(<ProjectsPage />)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'do the thing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plan' }))
    expect(await screen.findByTestId('project-detail')).toBeInTheDocument()
    expect(api.planTask).toHaveBeenCalledWith('do the thing', 'text', undefined, '', '')
  })

  it('threads the picked agent into planTask', async () => {
    renderWithProviders(<ProjectsPage />)
    fireEvent.change(await screen.findByLabelText('Agent picker'), { target: { value: 'reviewer' } })
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'audit the rail' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plan' }))
    await waitFor(() => expect(api.planTask).toHaveBeenCalledWith('audit the rail', 'text', undefined, 'reviewer', ''))
  })

  it('shows the backend error when planTask reports failure', async () => {
    vi.mocked(api.planTask).mockResolvedValue({ ok: false, error: 'decomposer refused' })
    renderWithProviders(<ProjectsPage />)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plan' }))
    expect(await screen.findByText('decomposer refused')).toBeInTheDocument()
  })

  it('shows a generic error when planTask reports failure with no message', async () => {
    vi.mocked(api.planTask).mockResolvedValue({ ok: false })
    renderWithProviders(<ProjectsPage />)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plan' }))
    expect(await screen.findByText('Failed to generate plan')).toBeInTheDocument()
  })

  it('shows the thrown message when the plan request rejects', async () => {
    vi.mocked(api.planTask).mockRejectedValue(new Error('network down'))
    renderWithProviders(<ProjectsPage />)
    fireEvent.change(screen.getByPlaceholderText('Describe your task...'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plan' }))
    expect(await screen.findByText('network down')).toBeInTheDocument()
  })

  it('cancels a planning run restored from sessionStorage', async () => {
    sessionStorage.setItem('tr-planning', '1')
    renderWithProviders(<ProjectsPage />)
    expect(screen.getByText(/Generating execution plan/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.cancelPlan).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByText(/Generating execution plan/)).not.toBeInTheDocument())
    expect(sessionStorage.getItem('tr-planning')).toBeNull()
  })

  it('ticks the planning banner dots', async () => {
    sessionStorage.setItem('tr-planning', '1')
    vi.useFakeTimers()
    renderWithProviders(<ProjectsPage />)
    const banner = screen.getByText(/Generating execution plan/)
    expect(banner.textContent).toBe('Generating execution plan')
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(banner.textContent).toBe('Generating execution plan.')
  })

  it('recovery poll adopts a planned run and stops planning', async () => {
    resetApi([mkRun({ task_id: 'rec-1', name: 'Recovered', status: 'planned' })])
    sessionStorage.setItem('tr-planning', '1')
    vi.useFakeTimers()
    renderWithProviders(<ProjectsPage />)
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(sessionStorage.getItem('tr-planning')).toBeNull()
    expect(screen.getByTestId('project-detail')).toBeInTheDocument()
    expect(screen.queryByText(/Generating execution plan/)).not.toBeInTheDocument()
  })

  it('recovery poll surfaces a failed run error instead of a plan', async () => {
    resetApi([mkRun({ task_id: 'rec-2', name: 'Broken', status: 'failed', error: 'planner exploded' })])
    sessionStorage.setItem('tr-planning', '1')
    vi.useFakeTimers()
    renderWithProviders(<ProjectsPage />)
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(sessionStorage.getItem('tr-planning')).toBeNull()
    expect(screen.getByText('planner exploded')).toBeInTheDocument()
  })

  it('the refresh interval does not stack a second load on an in-flight one', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>(r => { release = r })
    vi.mocked(api.taskRunnerStatus).mockImplementation(async () => {
      await gate
      return { running: false, available: true, runs: [] }
    })
    vi.useFakeTimers()
    renderWithProviders(<ProjectsPage />)
    await act(async () => { await vi.advanceTimersByTimeAsync(3100) })
    expect(api.taskRunnerStatus).toHaveBeenCalledTimes(1)
    release()
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  })
})

describe('ProjectsPage — applied deep link', () => {
  it('selects the run named by ?applied', async () => {
    resetApi([mkRun({ task_id: 'run-7', name: 'Applied Run' })])
    renderWithProviders(<ProjectsPage />, { route: '/projects?applied=run-7' })
    expect(await screen.findByTestId('project-detail')).toBeInTheDocument()
    expect(screen.getAllByText('Applied Run').length).toBeGreaterThan(1)
  })

  it('auto-executes a planned run when ?autoRun=true', async () => {
    resetApi([mkRun({ task_id: 'run-8', name: 'Auto Run', status: 'planned' })])
    renderWithProviders(<ProjectsPage />, { route: '/projects?applied=run-8&autoRun=true' })
    // Auto-run is a programmatic launch, never an affirmative trust grant.
    await waitFor(() => expect(api.executePlan).toHaveBeenCalledWith('run-8', '', false))
  })

  it('leaves an unknown ?applied id unselected', async () => {
    resetApi([mkRun({ task_id: 'run-9', name: 'Other Run' })])
    renderWithProviders(<ProjectsPage />, { route: '/projects?applied=missing-run' })
    await screen.findByText('Other Run')
    expect(screen.queryByTestId('project-detail')).not.toBeInTheDocument()
  })
})

describe('ProjectsPage — run rail selection', () => {
  it('selects a run from the keyboard and clears it again via New Task', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    fireEvent.keyDown(await screen.findByRole('button', { name: 'Open project Existing' }), { key: 'Enter' })
    expect(await screen.findByTestId('project-detail')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'New Task' }))
    expect(screen.getByPlaceholderText('Describe your task...')).toBeInTheDocument()
    expect(screen.queryByTestId('project-detail')).not.toBeInTheDocument()
  })

  it('selects a run when Space is pressed on the row', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    fireEvent.keyDown(await screen.findByRole('button', { name: 'Open project Existing' }), { key: ' ' })
    expect(await screen.findByTestId('project-detail')).toBeInTheDocument()
  })

  it('forwards a per-step retry from the detail pane', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    fireEvent.click(screen.getByRole('button', { name: 'detail retry step' }))
    await waitFor(() => expect(api.retryTaskRun).toHaveBeenCalledWith('run-1', 3))
  })

  it('forwards a refresh request from the detail pane', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    const before = vi.mocked(api.taskRunnerStatus).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'detail refresh' }))
    await waitFor(() => expect(vi.mocked(api.taskRunnerStatus).mock.calls.length).toBeGreaterThan(before))
  })
})

describe('ProjectsPage — renaming a run', () => {
  it('commits a new name on blur', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename project' })[0])
    const input = screen.getByLabelText('Project name') as HTMLInputElement
    expect(input.value).toBe('Existing')
    fireEvent.change(input, { target: { value: 'Renamed run' } })
    fireEvent.blur(input)
    await waitFor(() => expect(api.renameTaskRun).toHaveBeenCalledWith('run-1', 'Renamed run'))
  })

  it('does not call the rename endpoint when the name is unchanged', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename project' })[0])
    fireEvent.blur(screen.getByLabelText('Project name'))
    await waitFor(() => expect(screen.queryByLabelText('Project name')).not.toBeInTheDocument())
    expect(api.renameTaskRun).not.toHaveBeenCalled()
  })

  it('blurs on Enter and abandons the edit on Escape', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename project' })[0])
    fireEvent.keyDown(screen.getByLabelText('Project name'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Project name')).not.toBeInTheDocument())
    expect(api.renameTaskRun).not.toHaveBeenCalled()

    fireEvent.click(screen.getAllByRole('button', { name: 'Rename project' })[0])
    const input = screen.getByLabelText('Project name')
    fireEvent.change(input, { target: { value: 'Enter committed' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.renameTaskRun).toHaveBeenCalledWith('run-1', 'Enter committed'))
  })

  it('opens the editor from the pencil affordance, by click and by keyboard', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename project' })[1])
    expect(screen.getByLabelText('Project name')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByLabelText('Project name'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Project name')).not.toBeInTheDocument())

    fireEvent.keyDown(screen.getAllByRole('button', { name: 'Rename project' })[1], { key: ' ' })
    expect(screen.getByLabelText('Project name')).toBeInTheDocument()
  })

  it('opens the editor from the name itself via the keyboard', async () => {
    resetApi([mkRun()])
    renderWithProviders(<ProjectsPage />)
    await openRun()
    fireEvent.keyDown(screen.getAllByRole('button', { name: 'Rename project' })[0], { key: 'Enter' })
    expect(screen.getByLabelText('Project name')).toBeInTheDocument()
  })

  it('falls back to a placeholder name for an unnamed run', async () => {
    resetApi([mkRun({ name: '', spec_name: '' })])
    renderWithProviders(<ProjectsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open project run-1' }))
    await screen.findByTestId('project-detail')
    fireEvent.keyDown(screen.getAllByRole('button', { name: 'Rename project' })[0], { key: 'Enter' })
    expect((screen.getByLabelText('Project name') as HTMLInputElement).value).toBe('Project')
  })
})

describe('ProjectsPage — header actions for a planned run', () => {
  const planned = () => mkRun({ status: 'planned' })

  it('hands the plan to chat', async () => {
    resetApi([planned()])
    const { store } = renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Chat' }))
    await waitFor(() => expect(api.planContext).toHaveBeenCalledWith('run-1'))
    await waitFor(() => expect(store.getState().chat.pendingInput).toContain('PLAN CONTEXT'))
  })

  it('does not push to chat when no plan context comes back', async () => {
    resetApi([planned()])
    vi.mocked(api.planContext).mockResolvedValue({ ok: true, context: '' })
    const { store } = renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Chat' }))
    await waitFor(() => expect(api.planContext).toHaveBeenCalledTimes(1))
    expect(store.getState().chat.pendingInput).toBeNull()
  })

  it('discards the plan and returns to the compose panel', async () => {
    resetApi([planned()])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(api.deleteTaskRun).toHaveBeenCalledWith('run-1'))
    expect(await screen.findByPlaceholderText('Describe your task...')).toBeInTheDocument()
  })

  it('executes with the per-run auto-approve grant the user ticked', async () => {
    resetApi([planned()])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    const box = within(header).getByRole('checkbox') as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    fireEvent.click(within(header).getByRole('button', { name: 'Execute' }))
    await waitFor(() => expect(api.executePlan).toHaveBeenCalledWith('run-1', '', true))
  })
})

describe('ProjectsPage — header actions by run status', () => {
  it('cancels a run that is still planning', async () => {
    resetApi([mkRun({ status: 'planning' })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    expect(within(header).getByText('Planning…')).toBeInTheDocument()
    fireEvent.click(within(header).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.cancelPlan).toHaveBeenCalledTimes(1))
    expect(await screen.findByPlaceholderText('Describe your task...')).toBeInTheDocument()
  })

  it('pauses a running run', async () => {
    resetApi([mkRun({ status: 'running', running: true })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    expect(within(header).getByText('Running')).toBeInTheDocument()
    fireEvent.click(within(header).getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(api.pauseTaskRun).toHaveBeenCalledWith('run-1'))
  })

  it('cancels a running run from the header, not the rail', async () => {
    resetApi([mkRun({ status: 'running', running: true })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.cancelTaskRunner).toHaveBeenCalledWith('run-1'))
  })

  it('resumes a paused run and offers no Restart', async () => {
    resetApi([mkRun({ status: 'paused' })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    expect(within(header).queryByRole('button', { name: 'Restart' })).not.toBeInTheDocument()
    fireEvent.click(within(header).getByRole('checkbox'))
    fireEvent.click(within(header).getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.executePlan).toHaveBeenCalledWith('run-1', '', true))
  })

  it('shows a paused run unchecked even when it carries a stale auto-approve intent', async () => {
    resetApi([mkRun({ status: 'paused', auto_approve: true, auto_approve_remaining_secs: 0 })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    expect((within(header).getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
  })

  it('reflects a live auto-approve grant as checked', async () => {
    resetApi([mkRun({ status: 'paused', auto_approve_remaining_secs: 900 })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    expect((within(header).getByRole('checkbox') as HTMLInputElement).checked).toBe(true)
  })

  it('moves a completed run into a chat slot', async () => {
    resetApi([mkRun({ status: 'completed' })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Chat' }))
    await waitFor(() => expect(api.taskRunToChat).toHaveBeenCalledWith('run-1'))
  })

  it('stays put when the run cannot be moved into a slot', async () => {
    resetApi([mkRun({ status: 'cancelled' })])
    vi.mocked(api.taskRunToChat).mockResolvedValue({ slot: null })
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Chat' }))
    await waitFor(() => expect(api.taskRunToChat).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('project-detail')).toBeInTheDocument()
  })

  it('schedules a completed run as a daily cron job', async () => {
    resetApi([mkRun({ status: 'completed', spec_content: 'rebuild the index' })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Schedule' }))
    await waitFor(() => expect(api.createCron).toHaveBeenCalledWith({
      name: 'Project: Existing',
      message: 'run __inline__:rebuild the index',
      every: 86400,
    }))
    expect(alertSpy).toHaveBeenCalledWith('Scheduled as daily cron job')
  })

  it('refuses to schedule a run with nothing to re-run', async () => {
    resetApi([mkRun({ status: 'completed', spec_content: '', original_input: '' })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Schedule' }))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('No spec/idea to schedule'))
    expect(api.createCron).not.toHaveBeenCalled()
  })

  it('restarts a failed run from the first step', async () => {
    resetApi([mkRun({ status: 'failed', error: 'step 2 blew up' })])
    renderWithProviders(<ProjectsPage />)
    const header = await openRun()
    fireEvent.click(within(header).getByRole('button', { name: 'Restart' }))
    await waitFor(() => expect(api.retryTaskRun).toHaveBeenCalledWith('run-1', 1))
  })
})
