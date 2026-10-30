/**
 * The Task Runner page's "Chat" button keeps the review chat it opens through a
 * full `slots` frame that omits it.
 *
 * `/to-chat` mints a `task-review-*` session on the server and answers with its
 * key alone. A full frame serialized before that, or held in flight, can reach
 * the tab after the switch and drop the key, and the chat page then returns the
 * user to the chat they came from. Runs against the REAL reducers, because only
 * a membership add stamped in the dashboard reducer survives that frame.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { createTestStore, renderWithProviders } from './helpers'
import ProjectsPage from '../pages/ProjectsPage'
import { api } from '../api/client'
import { sseSlots } from '../store/dashboardSlice'
import { switchSlot } from '../store/chatSlice'
import type { ChatSlot, ProjectRun, RunStatus } from '../types'

vi.mock('../pages/ProjectDetailPage', () => ({
  default: () => <div data-testid="project-detail" />,
}))
vi.mock('../components/AgentSelector', () => ({ default: () => null }))
vi.mock('../api/client', () => ({
  api: {
    taskRunnerStatus: vi.fn(),
    kirocrewAgents: vi.fn(),
    agentCatalog: vi.fn(),
    refineStatus: vi.fn(),
    taskRunToChat: vi.fn(),
    chatSlotDetail: vi.fn(),
  },
}))

const run: ProjectRun = {
  task_id: 'run-1', name: 'Existing', running: false, status: 'cancelled' as RunStatus,
  tasks: 2, completed: 0, failed: 0, skipped: 0, current_task: 0,
  spec: '', spec_name: '', error: '', tokens_used: 0, replan_count: 0,
  task_details: [], started_at: 0, finished_at: 0,
  work_dir: '', branch_name: '', spec_content: 'spec body', lessons_learned: [],
  commits: 0, original_input: '', source: 'text', groups: [],
}

const row = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})
const wire = (...keys: string[]): ChatSlot[] => keys.map(row)

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.taskRunnerStatus).mockResolvedValue({ running: false, available: true, runs: [run] })
  vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [], default_agent: '' })
  vi.mocked(api.agentCatalog).mockResolvedValue({ agents: [], default_agent: '' })
  vi.mocked(api.refineStatus).mockResolvedValue({ status: 'idle', text: '', error: '' })
  vi.mocked(api.taskRunToChat).mockResolvedValue({ ok: true, slot: 'task-review-1' })
  vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [], total: 0 })
})

describe('ProjectsPage "Chat" and a stale slots frame', () => {
  it('keeps the review chat through a frame that omits it', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots(wire('origin', 'other')))
    // The user is in `origin` when they open the Task Runner page.
    await store.dispatch(switchSlot('origin'))
    renderWithProviders(<ProjectsPage />, { store })
    fireEvent.click(await screen.findByRole('button', { name: 'Open project Existing' }))
    await screen.findByTestId('project-detail')
    const header = screen.getByTestId('project-detail').parentElement!.parentElement!
      .querySelector<HTMLElement>('div.border-b')!
    fireEvent.click(within(header).getByRole('button', { name: 'Chat' }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('task-review-1'))
    const keys = () => store.getState().dashboard.slots.map(s => s.key)
    expect(keys()).toContain('task-review-1')

    store.dispatch(sseSlots(wire('origin', 'other')))

    expect(keys()).toContain('task-review-1')
    expect(store.getState().chat.activeSlot).toBe('task-review-1')
  })
})
