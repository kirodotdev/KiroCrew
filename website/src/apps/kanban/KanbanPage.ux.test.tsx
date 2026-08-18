/**
 * The four board behaviours a user notices when something goes wrong.
 *
 * Each of these was reachable-but-silent before: a refused mutation looked like
 * nothing happening, the detail modal froze at open time so a background settle
 * never reached it, closing it discarded an unsaved edit without asking, and the
 * counts concatenated a literal English 's'. Every case here drives the real
 * component and asserts what the user would see, not that a handler was called.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import KanbanPage from './KanbanPage'
import type { TaskRecord } from './types'

vi.mock('./api', () => ({
  fetchTasks: vi.fn(async () => ({ tasks: [], total: 0 })),
  createTask: vi.fn(async () => ({})),
  updateTask: vi.fn(async () => ({})),
  deleteTask: vi.fn(async () => undefined),
  moveTask: vi.fn(async () => ({})),
  runTask: vi.fn(async () => ({ status: 'running' })),
  reconcileTasks: vi.fn(async () => ({ reconciled: 0 })),
}))

import * as api from './api'

/** A complete record: a partial one makes the modal read fields that are absent. */
function task(over: Partial<TaskRecord> = {}): TaskRecord {
  return {
    id: 't1',
    title: 'Original title',
    description: 'Original description',
    prompt: 'do the thing',
    status: 'todo',
    tags: [],
    priority: 'medium',
    createdAt: 1,
    updatedAt: 1,
    executions: [],
    refining: false,
    ...over,
  } as TaskRecord
}

function mount() {
  // retry:false — a mutation that retries swallows the first rejection, and the
  // error surface under test is exactly what that first rejection produces.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <KanbanPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(api.fetchTasks as any).mockResolvedValue({ tasks: [], total: 0 })
  ;(api.reconcileTasks as any).mockResolvedValue({ reconciled: 0 })
})

describe('a refused mutation is shown, not swallowed', () => {
  it('surfaces the 409 when Run is refused', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    ;(api.runTask as any).mockRejectedValue(new Error('409: task_already_running'))
    mount()

    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByTitle('Run this task'))

    // The point is the user SEES it: an alert region carrying the server's reason.
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('409')
  })

  it('the banner is dismissible, so it never becomes permanent furniture', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    ;(api.runTask as any).mockRejectedValue(new Error('409: task_already_running'))
    mount()

    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByTitle('Run this task'))
    await screen.findByRole('alert')

    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })
})

describe('the detail modal reads the live record', () => {
  it('picks up a background title change instead of the snapshot it opened with', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task({ refining: true })], total: 1 })
    mount()

    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByText('Original title'))
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // The background namer lands a real title; the board's poll delivers it.
    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task({ title: 'Named by the model', refining: false })],
      total: 1,
    })

    await waitFor(
      () => {
        const field = screen.getByDisplayValue('Named by the model')
        expect(field).toBeTruthy()
      },
      { timeout: 4000 },
    )
  })

  it('closes itself when the card it was showing is gone', async () => {
    // refining -> the board polls at 1500ms, so the removal is seen promptly.
    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task({ refining: true })],
      total: 1,
    })
    mount()

    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByText('Original title'))
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Deleted from another surface: the board no longer returns it.
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [], total: 0 })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 4000 })
  })
})

describe('an unsaved edit is not discarded silently', () => {
  async function openWithAnEdit() {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    mount()
    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByText('Original title'))
    await screen.findByRole('dialog')
    fireEvent.change(screen.getByDisplayValue('Original title'), {
      target: { value: 'Half-typed edit' },
    })
  }

  it('Escape asks before throwing the edit away', async () => {
    await openWithAnEdit()
    fireEvent.keyDown(window, { key: 'Escape' })

    expect(await screen.findByRole('alertdialog')).toBeTruthy()
    // Still open: Escape asked, it did not close.
    expect(screen.queryByRole('dialog')).toBeTruthy()
  })

  it('Keep editing returns to the form with the edit intact', async () => {
    await openWithAnEdit()
    fireEvent.keyDown(window, { key: 'Escape' })
    await screen.findByRole('alertdialog')

    fireEvent.click(screen.getByRole('button', { name: /keep editing/i }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(screen.getByDisplayValue('Half-typed edit')).toBeTruthy()
  })

  it('Discard is the only path that actually loses it', async () => {
    await openWithAnEdit()
    fireEvent.keyDown(window, { key: 'Escape' })
    await screen.findByRole('alertdialog')

    fireEvent.click(screen.getByRole('button', { name: /^discard$/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('with nothing edited, Escape closes straight away', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    mount()
    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByText('Original title'))
    await screen.findByRole('dialog')

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})

describe('the counts are pluralised by the catalog, not by string concatenation', () => {
  it('reads singular at one task and plural at two', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    const first = mount()
    await waitFor(() => expect(screen.getByText('1 task')).toBeTruthy())
    first.unmount()

    ;(api.fetchTasks as any).mockResolvedValue({
      tasks: [task(), task({ id: 't2', title: 'Second' })],
      total: 2,
    })
    mount()
    await waitFor(() => expect(screen.getByText('2 tasks')).toBeTruthy())
  })
})

describe('the create form dismisses on submit, not on the response', () => {
  it('closes while the POST is still in flight', async () => {
    // Creating a card never waits on a model, so the form has nothing to wait
    // for. Holding it open until the response lands left the user looking at a
    // filled-in form with no feedback, and invited a second submit.
    let release: (v: unknown) => void = () => {}
    ;(api.createTask as any).mockImplementation(
      () => new Promise((resolve) => { release = resolve }),
    )
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'New Task' }))
    const box = await screen.findByPlaceholderText(/Describe your task/i)
    fireEvent.change(box, { target: { value: 'upgrade the deps' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create Task' }))

    // Still unresolved: the form must already be gone.
    await waitFor(() =>
      expect(screen.queryByPlaceholderText(/Describe your task/i)).toBeNull(),
    )
    expect((api.createTask as any).mock.calls[0][0]).toEqual({
      prompt: 'upgrade the deps',
      status: 'todo',
    })
    release({})
  })

  it('hands the prompt back when the create fails, instead of destroying it', async () => {
    // Dismissing on submit must not cost the user their typing: a failed POST
    // re-opens the form with the prompt still in it, next to the banner. A banner
    // alone would report the failure after the words were already gone.
    ;(api.createTask as any).mockRejectedValue(new Error('500: execution_start_failed'))
    mount()

    fireEvent.click(await screen.findByRole('button', { name: 'New Task' }))
    fireEvent.change(await screen.findByPlaceholderText(/Describe your task/i), {
      target: { value: 'upgrade the deps' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create Task' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('500')
    const restored = await screen.findByPlaceholderText(/Describe your task/i)
    expect((restored as HTMLTextAreaElement).value).toBe('upgrade the deps')
  })

  it('starts blank again after a successful create', async () => {
    // The restored draft is failure-scoped; a later open must not resurrect it.
    ;(api.createTask as any).mockRejectedValue(new Error('500: nope'))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'New Task' }))
    fireEvent.change(await screen.findByPlaceholderText(/Describe your task/i), {
      target: { value: 'first attempt' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create Task' }))
    await screen.findByRole('alert')

    ;(api.createTask as any).mockResolvedValue({})
    fireEvent.click(screen.getByRole('button', { name: 'Create Task' }))
    await waitFor(() =>
      expect(screen.queryByPlaceholderText(/Describe your task/i)).toBeNull(),
    )

    fireEvent.click(screen.getByRole('button', { name: 'New Task' }))
    const reopened = await screen.findByPlaceholderText(/Describe your task/i)
    expect((reopened as HTMLTextAreaElement).value).toBe('')
  })
})

describe('the detail modal backdrop is reachable without a mouse', () => {
  it('exposes click-away as a named button that Enter activates', async () => {
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    mount()

    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByText('Original title'))
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // A plain clickable div gave keyboard users no way to reach this gesture.
    const backdrop = screen.getByRole('button', { name: 'Close task detail' })
    fireEvent.keyDown(backdrop, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps the dialog OUTSIDE the backdrop button', async () => {
    // Wrapping the dialog in the clickable would put every control it owns
    // inside a role="button" -- the trap SideSheet documents.
    ;(api.fetchTasks as any).mockResolvedValue({ tasks: [task()], total: 1 })
    mount()

    await waitFor(() => expect(screen.getByText('Original title')).toBeTruthy())
    fireEvent.click(screen.getByText('Original title'))

    const dialog = await screen.findByRole('dialog')
    const backdrop = screen.getByRole('button', { name: 'Close task detail' })
    expect(backdrop.contains(dialog)).toBe(false)
  })
})
