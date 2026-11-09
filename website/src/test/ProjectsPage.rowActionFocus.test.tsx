/**
 * Projects list row action (Cancel / Delete): the icon button keeps keyboard
 * focus while its request is in flight.
 *
 * A native disabled button leaves the tab order, so disabling the button for
 * the request dropped the focus of the user who pressed it to <body>. It is
 * aria-disabled (and aria-busy) instead, and a second press is ignored until
 * the request ends.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { i18nT } from '../i18n/t'
import type { ProjectRun } from '../types'

vi.mock('../pages/ProjectDetailPage', () => ({ default: () => <div data-testid="project-detail">Detail</div> }))
vi.mock('../components/AgentSelector', () => ({
  default: ({ value, onChange }: { value: string; onChange: (name: string) => void }) => (
    <select data-testid="agent-select" value={value} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value)}>
      <option value="">default</option>
    </select>
  ),
}))

const { cancelMock } = vi.hoisted(() => ({
  cancelMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'cancelTaskRunner') return cancelMock
      if (prop === 'taskRunnerStatus') {
        return vi.fn().mockResolvedValue({ running: true, available: true, runs: [RUN] })
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

const RUN_ID = 'task-running-1'

const RUN: ProjectRun = {
  task_id: RUN_ID, name: 'My Running Project', running: true, status: 'running',
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
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ProjectsPage />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  cancelMock.mockReset()
})

describe('ProjectsPage row action — focus while in flight', () => {
  it('keeps focus on the Cancel button, marks it aria-disabled/aria-busy, and ignores a second press until the request ends', async () => {
    let finish: (v: { ok: boolean }) => void = () => {}
    cancelMock.mockImplementation(() => new Promise<{ ok: boolean }>(r => { finish = r }))

    renderPage()
    const btn = await screen.findByRole('button', { name: i18nT('pages.projectsPage.cancel') })

    act(() => { btn.focus() })
    act(() => { fireEvent.click(btn) })
    act(() => { fireEvent.click(btn) })

    // One in-flight request; the second press is ignored while busy.
    expect(cancelMock).toHaveBeenCalledTimes(1)
    // Focusable, not removed from the tab order.
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn).toHaveFocus()
    expect(document.activeElement).not.toBe(document.body)

    await act(async () => { finish({ ok: true }) })

    // Once the request ends the button is interactive again.
    await waitFor(() => expect(btn).toHaveAttribute('aria-disabled', 'false'))
    act(() => { fireEvent.click(btn) })
    expect(cancelMock).toHaveBeenCalledTimes(2)
  })
})
