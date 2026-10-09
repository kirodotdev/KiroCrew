/**
 * Evidence for the Project Detail page's task-gate refusals.
 *
 * Mounts the REAL ProjectDetailPage against the real store, stylesheet, theme
 * tokens and i18n catalog, with a running run whose two tasks are both at
 * their approval gates. The capture script answers `/api/approvals` the way
 * the gateway does: task 1's gate is listed without an instance (an older
 * build), so it names no request and the page leaves it out. Task 2's gate is listed live but the server refuses its
 * decide as gone; a press shows the shared refusal sentence.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ProjectDetailPage from '../src/pages/ProjectDetailPage'
import type { ProjectRun, TaskDetail } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const step = (over: Partial<TaskDetail>): TaskDetail => ({
  index: 1, title: '', description: '', status: 'pending', error: '', result: '',
  attempts: 0, depends_on: [], requires_approval: true, ...over,
})
const run: ProjectRun = {
  task_id: 'run-1', name: 'Release prep', running: true, status: 'running',
  tasks: 3, completed: 0, failed: 0, skipped: 0, current_task: 1,
  spec: 'release.md', spec_name: 'Release prep', error: '',
  tokens_used: 0, replan_count: 0,
  started_at: Date.now() / 1000 - 600, finished_at: 0,
  work_dir: '/workspace/release', branch_name: 'release-prep', spec_content: '# Release prep',
  lessons_learned: [], commits: 0, original_input: 'Prepare the release', source: 'text',
  groups: [[1, 2], [3]],
  task_details: [
    step({ index: 1, title: 'Migrate the database', status: 'in_progress' }),
    step({ index: 2, title: 'Rotate the API keys', status: 'in_progress' }),
    step({ index: 3, title: 'Publish the release notes', status: 'pending', requires_approval: false, depends_on: [1, 2] }),
  ],
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root className="h-screen bg-bg text-text flex flex-col">
          <ProjectDetailPage run={run} onRetry={() => {}} onRefresh={() => {}} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
