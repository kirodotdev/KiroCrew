/**
 * The board page's data wiring and its client-side search.
 *
 * The page owns three things worth pinning. It reconciles on mount, because a
 * gateway restart leaves tasks stranded in `running` and only that call settles
 * them. Its search filters across title, description, prompt AND tags, so a
 * user who remembers only a tag still finds the task. And the create flow
 * refines the prompt before creating, so a failed refine must not leave the
 * modal stuck in its "refining" state with every control disabled.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskRecord } from './types'

/**
 * The shape the real endpoints return. `updateTask` feeds
 * their result straight into `setSelectedTask`, so a partial stub here would
 * re-render the detail modal with a task that has no `executions` — a crash
 * that says nothing about the page.
 */
const RECORD = {
  id: 't1',
  title: 'Fix the cache',
  description: '',
  prompt: '',
  status: 'todo',
  created_at: 1,
  updated_at: 2,
  executions: [],
  tags: [],
  priority: 'medium',
}

vi.mock('./api', () => ({
  reconcileTasks: vi.fn(async () => ({ reconciled: 0 })),
  fetchTasks: vi.fn(async () => ({ tasks: [], total: 0 })),
  createTask: vi.fn(async () => ({ ...RECORD })),
  updateTask: vi.fn(async () => ({ ...RECORD })),
  deleteTask: vi.fn(async () => undefined),
  moveTask: vi.fn(async () => ({ ...RECORD })),
  runTask: vi.fn(async () => ({ execution_id: 'e1', session_key: null, status: 'running' })),
}))

const api = await import('./api')
const { default: KanbanPage } = await import('./KanbanPage')

function task(over: Partial<TaskRecord> = {}): TaskRecord {
  return {
    id: 't1',
    title: 'Fix the cache',
    description: '',
    prompt: '',
    status: 'todo',
    created_at: 1,
    updated_at: 1,
    executions: [],
    tags: [],
    priority: 'medium',
    ...over,
  }
}

/**
 * Renders the router's current location so a navigation has an observable
 * effect. `handleOpenSession` navigates instead of mutating, so without a probe
 * the only thing a click could assert is that nothing threw — which passes just
 * as happily when the handler is never reached.
 */
function LocationProbe() {
  const loc = useLocation()
  return <span data-testid="loc">{loc.pathname + loc.search}</span>
}

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <KanbanPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(api.reconcileTasks as any).mockResolvedValue({ reconciled: 0 })
  ;(api.fetchTasks as any).mockResolvedValue({ tasks: [], total: 0 })
})

describe('on mount', () => {
  it('reconciles stranded running tasks', async () => {
    mount()
    await waitFor(() => expect(api.reconcileTasks).toHaveBeenCalled())
  })

  it('survives a failed reconcile, since it is best-effort', async () => {
    ;(api.reconcileTasks as any).mockRejectedValue(new Error('offline'))
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
  })

  it('reconciles exactly once, not once per render', async () => {
    // The sweep runs through a React Query mutation rather than a `.then()` in an
    // effect, so React Query owns the invalidation — but a mutation fired from an
    // effect re-fires on every render whose deps moved, and reconcile settles
    // cards. Once per mount is the contract; the ref guard also absorbs
    // StrictMode's deliberate double-invoke.
    mount()
    await waitFor(() => expect(api.reconcileTasks).toHaveBeenCalledTimes(1))
    // Typing re-renders the page several times over.
    await userEvent.type(await screen.findByPlaceholderText(/search/i), 'abc')
    expect(api.reconcileTasks).toHaveBeenCalledTimes(1)
  })

  it('a failed reconcile does not raise the action banner', async () => {
    // The banner is for writes the USER asked for. A background repair that
    // failed is not something they can act on, and the board keeps polling.
    ;(api.reconcileTasks as any).mockRejectedValue(new Error('offline'))
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('loads the board', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task({ title: 'Loaded task' })],
      total: 1,
    })
    mount()
    expect(await screen.findByText('Loaded task')).toBeInTheDocument()
  })
})

describe('search', () => {
  async function withTasks(tasks: TaskRecord[]) {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks, total: tasks.length })
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
    return screen.getByPlaceholderText(/search/i)
  }

  it('matches on the title', async () => {
    const box = await withTasks([
      task({ id: 'a', title: 'cache work' }),
      task({ id: 'b', title: 'unrelated' }),
    ])
    await userEvent.type(box, 'cache')
    await waitFor(() => expect(screen.queryByText('unrelated')).not.toBeInTheDocument())
    expect(screen.getByText('cache work')).toBeInTheDocument()
  })

  it('matches on a tag the title does not mention', async () => {
    const box = await withTasks([
      task({ id: 'a', title: 'opaque name', tags: ['infra'] }),
      task({ id: 'b', title: 'other' }),
    ])
    await userEvent.type(box, 'infra')
    await waitFor(() => expect(screen.queryByText('other')).not.toBeInTheDocument())
    expect(screen.getByText('opaque name')).toBeInTheDocument()
  })

  it('matches on the prompt', async () => {
    const box = await withTasks([
      task({ id: 'a', title: 'opaque', prompt: 'rebuild the index' }),
      task({ id: 'b', title: 'other' }),
    ])
    await userEvent.type(box, 'rebuild')
    await waitFor(() => expect(screen.queryByText('other')).not.toBeInTheDocument())
  })

  it('a blank search shows everything again', async () => {
    const box = await withTasks([task({ id: 'a', title: 'one' }), task({ id: 'b', title: 'two' })])
    await userEvent.type(box, 'one')
    await waitFor(() => expect(screen.queryByText('two')).not.toBeInTheDocument())
    await userEvent.clear(box)
    expect(await screen.findByText('two')).toBeInTheDocument()
  })
})

describe('creating a task', () => {
  it('creates from the raw prompt without waiting on a refine', async () => {
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('button', { name: /new task/i }))
    await userEvent.type(screen.getByRole('textbox', { name: '' }), 'add a health check')
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    await userEvent.click(submit)
    await waitFor(() => expect(api.createTask).toHaveBeenCalled())
    // The whole point of the split: the create call carries the raw prompt and
    // no title, so the server derives one and names the card in the background.
    expect((api.createTask as any).mock.calls[0][0]).toMatchObject({
      prompt: 'add a health check',
      status: 'todo',
    })
    expect((api.createTask as any).mock.calls[0][0].title).toBeUndefined()
  })

  it('closes the form as soon as the card is created', async () => {
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('button', { name: /new task/i }))
    await userEvent.type(screen.getByRole('textbox', { name: '' }), 'something useful')
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    await userEvent.click(submit)
    await waitFor(() => expect(api.createTask).toHaveBeenCalled())
    // No spinner state to sit through: the modal is gone on the next tick.
    await waitFor(() => expect(screen.queryByRole('textbox', { name: '' })).toBeNull())
  })

  it('sends an over-long prompt through untruncated — the backend names it', async () => {
    const long = 'x'.repeat(80)
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('button', { name: /new task/i }))
    await userEvent.type(screen.getByRole('textbox', { name: '' }), long)
    const submit = screen.getAllByRole('button').find((b) => b.getAttribute('type') === 'submit')!
    await userEvent.click(submit)
    await waitFor(() => expect(api.createTask).toHaveBeenCalled())
    const arg = (api.createTask as any).mock.calls[0][0]
    // Truncation is the server's provisional-title concern now; the client must
    // not silently shorten what the user asked for.
    expect(arg.prompt).toBe(long)
    expect(arg.title).toBeUndefined()
  })
})

describe('the detail modal wiring', () => {
  async function openDetail() {
    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task({ title: 'Openable', executions: [{ id: 'e1', started_at: 1, session_key: 'sess-3' }] })],
      total: 1,
    })
    mount()
    await screen.findByText('Openable')
    await userEvent.click(screen.getByText('Openable'))
    return screen.getByDisplayValue('Openable')
  }

  it('opens a task and closes it again', async () => {
    await openDetail()
    // Escape is the modal's documented close path and does not depend on
    // guessing which rendered button is the X.
    await userEvent.keyboard('{Escape}')
    await waitFor(() =>
      expect(screen.queryByDisplayValue('Openable')).not.toBeInTheDocument(),
    )
  })

  it('saves an edit through the update mutation', async () => {
    const title = await openDetail()
    await userEvent.clear(title)
    await userEvent.type(title, 'Edited')
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(api.updateTask).toHaveBeenCalled())
    expect((api.updateTask as any).mock.calls[0][0]).toBe('t1')
  })

  it('deletes through the delete mutation after confirming', async () => {
    await openDetail()
    await userEvent.click(screen.getByRole('button', { name: /^delete$/i }))
    await userEvent.click(screen.getByRole('button', { name: /really delete/i }))
    await waitFor(() => expect(api.deleteTask).toHaveBeenCalled())
  })

  it('navigates to the chat session for a run', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task({ title: 'Ran', executions: [{ id: 'e1', started_at: 1, session_key: 'sess-3' }] })],
      total: 1,
    })
    mount()
    await screen.findByText('Ran')
    // The card's own session button, not the modal's — the card is what a user
    // reaches first, and it is the path that carries the slot key.
    await userEvent.click(screen.getByTitle('Open the agent session for this run'))
    // The slot key travels in `sid`; the path slug is cosmetic.
    await waitFor(() =>
      expect(screen.getByTestId('loc')).toHaveTextContent('/chat?sid=sess-3'),
    )
  })

  it('runs a task from its card', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task({ title: 'Runnable' })],
      total: 1,
    })
    mount()
    await screen.findByText('Runnable')
    await userEvent.click(screen.getByTitle('Run this task'))
    await waitFor(() => expect(api.runTask).toHaveBeenCalledWith('t1'))
  })

  it('closes the create form on cancel', async () => {
    mount()
    await waitFor(() => expect(api.fetchTasks).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('button', { name: /new task/i }))
    expect(screen.getByRole('textbox', { name: '' })).toBeInTheDocument()
    const cancel = screen
      .getAllByRole('button')
      .filter((b) => b.getAttribute('type') === 'button')
      .pop()!
    await userEvent.click(cancel)
    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: '' })).not.toBeInTheDocument(),
    )
  })
})
