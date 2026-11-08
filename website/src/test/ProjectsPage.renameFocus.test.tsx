/**
 * #13598 — Project rename: closing the inline editor never strands focus on
 * document.body.
 *
 * Enter and Escape both restore focus to the title trigger (the span with
 * role="button" that opened the editor). A pointer blur moves focus nowhere —
 * redirecting focus off the element the user chose is the context change
 * WCAG 3.2.2 forbids. A confirmed rename is announced through the polite
 * status region.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ProjectRun } from '../types'

vi.mock('../pages/ProjectDetailPage', () => ({ default: () => <div data-testid="project-detail">Detail</div> }))
vi.mock('../components/AgentSelector', () => ({
  default: ({ value, onChange }: { value: string; onChange: (name: string) => void }) => (
    <select data-testid="agent-select" value={value} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value)}>
      <option value="">default</option>
    </select>
  ),
}))

const { renameTaskRunMock } = vi.hoisted(() => ({
  renameTaskRunMock: vi.fn().mockResolvedValue({ ok: true }),
}))

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'renameTaskRun') return renameTaskRunMock
      if (prop === 'taskRunnerStatus') {
        return vi.fn().mockResolvedValue({
          running: false, available: true, runs: [RUN],
        })
      }
      return vi.fn().mockResolvedValue({ ok: true })
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

const RUN_ID = 'task-focus-1'
const PROJECT_NAME = 'My Alpha Project'

const RUN: ProjectRun = {
  task_id: RUN_ID, name: PROJECT_NAME, running: false, status: 'planned',
  tasks: 1, completed: 0, failed: 0, skipped: 0, current_task: 0,
  spec: '', spec_name: 'spec', error: '', tokens_used: 0, replan_count: 0,
  task_details: [], started_at: 0, finished_at: 0,
  work_dir: '', branch_name: '', spec_content: '', lessons_learned: [],
  commits: 0, original_input: '', source: 'text', groups: [],
}

import ProjectsPage from '../pages/ProjectsPage'

function renderPage() {
  const store = createTestStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ProjectsPage />
            <button type="button" data-testid="elsewhere">Elsewhere</button>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return view
}

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

/** Open the rename input by:
 * 1. Clicking the run item in the rail to select the run
 * 2. Clicking the project name trigger to open the rename editor
 */
async function openRenameInput() {
  // Click the rail item to select the run (accessible name = "Open project ...")
  const runItem = await screen.findByRole('button', { name: /Open project/ })
  act(() => { fireEvent.click(runItem) })
  // Click the title trigger to open rename (accessible name = "Rename project")
  const nameTrigger = await screen.findByTestId('project-name-trigger')
  act(() => { fireEvent.click(nameTrigger) })
  return screen.getByLabelText('Project name') as HTMLInputElement
}

const statusRegion = () => screen.queryByTestId('project-rename-status')

beforeEach(() => {
  renameTaskRunMock.mockReset()
  renameTaskRunMock.mockResolvedValue({ ok: true })
})

describe('ProjectsPage project rename — focus contract (#13598)', () => {
  it('status region is present and polite before any rename', async () => {
    renderPage()
    await screen.findByText(PROJECT_NAME)
    const region = statusRegion()
    expect(region).toBeTruthy()
    expect(region?.getAttribute('role')).toBe('status')
    expect(region?.getAttribute('aria-live')).toBe('polite')
    expect(region?.textContent).toBe('')
  })

  it('Enter commits and returns focus to the title trigger', async () => {
    renderPage()
    const input = await openRenameInput()
    act(() => { fireEvent.change(input, { target: { value: 'Renamed Project' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    await nextFrame()
    const trigger = screen.getByTestId('project-name-trigger')
    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('Enter with unchanged draft still returns focus to the trigger', async () => {
    renderPage()
    const input = await openRenameInput()
    // Unchanged draft — no API call, but focus must still return.
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    await nextFrame()
    const trigger = screen.getByTestId('project-name-trigger')
    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('Escape cancels and returns focus to the title trigger', async () => {
    renderPage()
    const input = await openRenameInput()
    act(() => { fireEvent.change(input, { target: { value: 'Abandoned' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    await nextFrame()
    expect(renameTaskRunMock).not.toHaveBeenCalled()
    const trigger = screen.getByTestId('project-name-trigger')
    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('pointer blur commits but does not redirect focus', async () => {
    renderPage()
    const input = await openRenameInput()
    act(() => { fireEvent.change(input, { target: { value: 'Clicked Away' } }) })
    // The user clicked another element — focus goes there, not the trigger.
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    await nextFrame()
    expect(renameTaskRunMock).toHaveBeenCalled()
    // Focus stays on the element the user chose, not the trigger.
    expect(document.activeElement).toBe(elsewhere)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('confirmed rename is announced through the polite status region', async () => {
    renderPage()
    const input = await openRenameInput()
    act(() => { fireEvent.change(input, { target: { value: 'Announced Name' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    await waitFor(() => expect(statusRegion()?.textContent).toBe('Project renamed to Announced Name'))
  })
})
