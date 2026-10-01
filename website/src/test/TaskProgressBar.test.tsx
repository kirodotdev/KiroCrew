import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import TaskProgressBar from '../pages/chat/TaskProgressBar'
import dashboardReducer, { sseTodoUpdate, sseSlots } from '../store/dashboardSlice'
import type { ChatSlot, TodoList } from '../types'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ApiError: actual.ApiError, api: { setTodoTask: vi.fn().mockResolvedValue({ ok: true }) } }
})

const todo = (tasks: Array<[string, boolean]>, description = 'Config workflow'): TodoList => {
  const list = tasks.map(([text, completed], i) => ({ id: String(i + 1), text, completed }))
  const completed = list.filter(t => t.completed).length
  return {
    description,
    tasks: list,
    completed,
    total: list.length,
    current: list.find(t => !t.completed)?.text ?? '',
  }
}

const slot = (key: string, t: TodoList | null): ChatSlot =>
  ({ key, messages: 0, running: false, todo: t }) as ChatSlot

function renderBar(slots: ChatSlot[], activeKey: string | null = 'slot-1') {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer },
    preloadedState: { dashboard: { slots } } as never,
  })
  const utils = render(
    <Provider store={store}>
      <TaskProgressBar slot={activeKey} />
    </Provider>,
  )
  return { store, ...utils }
}

describe('TaskProgressBar', () => {
  it('renders nothing when the slot has no todo list', () => {
    renderBar([slot('slot-1', null)])
    expect(screen.queryByTestId('todo-pill')).toBeNull()
  })

  it('renders nothing when the list is present but empty', () => {
    renderBar([slot('slot-1', todo([]))])
    expect(screen.queryByTestId('todo-pill')).toBeNull()
  })

  it('renders nothing when no slot is active', () => {
    renderBar([slot('slot-1', todo([['a', false]]))], null)
    expect(screen.queryByTestId('todo-pill')).toBeNull()
  })

  it('shows the server-derived count as "N of M"', () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', false], ['c', false]]))])
    expect(screen.getByTestId('todo-count').textContent).toBe('1 of 3')
  })

  it('shows the first incomplete task as the current task', () => {
    renderBar([slot('slot-1', todo([['done it', true], ['do this next', false]]))])
    expect(screen.getByTestId('todo-current').textContent).toBe('do this next')
  })

  it('reports completion instead of a current task when all are done', () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', true]]))])
    expect(screen.getByTestId('todo-count').textContent).toBe('2 of 2')
    expect(screen.getByTestId('todo-current').textContent).toBe('All tasks complete')
  })

  it('stays visible once every task is complete', () => {
    // Deliberate UX choice: the finished list is the payoff, not noise to hide.
    renderBar([slot('slot-1', todo([['a', true]]))])
    expect(screen.getByTestId('todo-pill')).toBeTruthy()
  })

  it('is collapsed initially and expands the full list on click', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', todo([['alpha', true], ['beta', false]]))])
    expect(screen.queryByTestId('todo-list')).toBeNull()
    const pill = screen.getByTestId('todo-pill')
    expect(pill.getAttribute('aria-expanded')).toBe('false')

    await user.click(pill)
    expect(screen.getByTestId('todo-list')).toBeTruthy()
    expect(pill.getAttribute('aria-expanded')).toBe('true')
    const rows = screen.getAllByTestId('todo-row')
    expect(rows).toHaveLength(2)
    // Scoped to the rows: 'beta' also appears in the pill's current-task label,
    // so a document-wide text query would be ambiguous by design.
    expect(rows.map(r => r.textContent)).toEqual(['alpha', 'beta'])
  })

  it('collapses again on a second click', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', todo([['alpha', false]]))])
    const pill = screen.getByTestId('todo-pill')
    await user.click(pill)
    expect(screen.getByTestId('todo-list')).toBeTruthy()
    await user.click(pill)
    expect(screen.queryByTestId('todo-list')).toBeNull()
  })

  it('reads only the active slot\'s list', () => {
    renderBar(
      [slot('slot-1', todo([['mine', false]])), slot('slot-2', todo([['theirs', false], ['other', false]]))],
      'slot-1',
    )
    expect(screen.getByTestId('todo-count').textContent).toBe('0 of 1')
    expect(screen.getByTestId('todo-current').textContent).toBe('mine')
  })

  it('exposes an accessible progressbar matching the count', () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', false], ['c', false], ['d', false]]))])
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('1')
    expect(bar.getAttribute('aria-valuemax')).toBe('4')
  })

  it('tolerates a partial state slice with no slots key', () => {
    // Fixtures across the suite build partial preloaded state; an undefined
    // slots array must not throw.
    const store = configureStore({
      reducer: { dashboard: dashboardReducer },
      preloadedState: { dashboard: {} } as never,
    })
    expect(() =>
      render(
        <Provider store={store}>
          <TaskProgressBar slot="slot-1" />
        </Provider>,
      ),
    ).not.toThrow()
  })

  it('updates live when a todo_update delta arrives', () => {
    const { store } = renderBar([slot('slot-1', todo([['a', false], ['b', false]]))])
    expect(screen.getByTestId('todo-count').textContent).toBe('0 of 2')
    act(() => { store.dispatch(sseTodoUpdate({ slot: 'slot-1', todo: todo([['a', true], ['b', false]]) })) })
    expect(screen.getByTestId('todo-count').textContent).toBe('1 of 2')
    expect(screen.getByTestId('todo-current').textContent).toBe('b')
  })

  it('rehydrates from a slots snapshot after reconnect', () => {
    // The snapshot path is what makes the pill survive a refresh mid-turn.
    const { store } = renderBar([slot('slot-1', null)])
    expect(screen.queryByTestId('todo-pill')).toBeNull()
    act(() => { store.dispatch(sseSlots([slot('slot-1', todo([['a', true], ['b', false]]))])) })
    expect(screen.getByTestId('todo-count').textContent).toBe('1 of 2')
  })
})

describe('TaskProgressBar row ticking', () => {
  beforeEach(() => {
    vi.mocked(api.setTodoTask).mockReset()
    vi.mocked(api.setTodoTask).mockResolvedValue({ ok: true })
  })

  it('PATCHes the clicked row with the opposite completed flag', async () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    const rows = screen.getAllByTestId('todo-row-toggle')
    expect(rows[0]).toHaveAttribute('aria-checked', 'true')
    expect(rows[1]).toHaveAttribute('aria-checked', 'false')
    await userEvent.click(rows[1])
    expect(api.setTodoTask).toHaveBeenCalledWith('slot-1', '2', 'b', true)
    await userEvent.click(rows[0])
    expect(api.setTodoTask).toHaveBeenCalledWith('slot-1', '1', 'a', false)
  })

  it('says at rest that rows are clickable and names the outcome on each row', async () => {
    renderBar([slot('slot-1', todo([['a', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    expect(screen.getByTestId('todo-hint').textContent).toMatch(/mark it done/i)
    expect(screen.getByTestId('todo-row-toggle')).toHaveAttribute('title', 'Mark done: a. The agent will not redo it. Click again to undo.')
  })

  it('does not repaint from the PATCH response: the gateway echo is the one ordered source', async () => {
    vi.mocked(api.setTodoTask).mockResolvedValue({ ok: true, todo: todo([['a', true]]) })
    renderBar([slot('slot-1', todo([['a', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    await userEvent.click(screen.getByTestId('todo-row-toggle'))
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(1))
    // Two quick clicks could otherwise resolve out of order and an older
    // response restore an obsolete list; only the serialized echo repaints.
    expect(screen.getByTestId('todo-row-toggle')).toHaveAttribute('aria-checked', 'false')
  })

  it('repaints from the gateway echo', async () => {
    const { store } = renderBar([slot('slot-1', todo([['a', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    act(() => { store.dispatch(sseTodoUpdate({ slot: 'slot-1', todo: todo([['a', true]]) })) })
    expect(screen.getByTestId('todo-row-toggle')).toHaveAttribute('aria-checked', 'true')
  })

  it('queues clicks and sends them one at a time, so a batch on a slow link is not dropped', async () => {
    const resolvers: Array<(v: unknown) => void> = []
    vi.mocked(api.setTodoTask).mockImplementation(() => new Promise(r => { resolvers.push(r) }))
    renderBar([slot('slot-1', todo([['a', false], ['b', false], ['c', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    const rows = screen.getAllByTestId('todo-row-toggle')
    await userEvent.click(rows[0])
    await userEvent.click(rows[1])
    await userEvent.click(rows[2])
    // One PATCH in flight; the other two wait, and every clicked row is held.
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(1))
    expect(rows[0]).toBeDisabled()
    expect(rows[1]).toBeDisabled()
    expect(rows[2]).toBeDisabled()
    // Each held row says so in visible text: the dimmed glyph alone read as hovered.
    expect(screen.getAllByTestId('todo-row-pending')).toHaveLength(3)
    expect(screen.getAllByTestId('todo-row-pending')[0].textContent).toBe('Sending')
    act(() => { resolvers[0]({ ok: true }) })
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(2))
    expect(api.setTodoTask).toHaveBeenLastCalledWith('slot-1', '2', 'b', true)
    act(() => { resolvers[1]({ ok: true }) })
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(3))
    expect(api.setTodoTask).toHaveBeenLastCalledWith('slot-1', '3', 'c', true)
    act(() => { resolvers[2]({ ok: true }) })
    await waitFor(() => expect(rows[2]).not.toBeDisabled())
  })

  it('a queued tick PATCHes the slot it was clicked in, not the slot shown after a switch', async () => {
    const resolvers: Array<(v: unknown) => void> = []
    vi.mocked(api.setTodoTask).mockImplementation(() => new Promise(r => { resolvers.push(r) }))
    const { store, rerender } = renderBar([
      slot('slot-1', todo([['a', false], ['b', false]])),
      slot('slot-2', todo([['x', false], ['y', false]])),
    ])
    await userEvent.click(screen.getByTestId('todo-pill'))
    const rows = screen.getAllByTestId('todo-row-toggle')
    await userEvent.click(rows[0])
    await userEvent.click(rows[1])
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(1))
    // The person switches sessions while row 2's tick is still queued.
    rerender(<Provider store={store}><TaskProgressBar slot="slot-2" /></Provider>)
    act(() => { resolvers[0]({ ok: true }) })
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(2))
    expect(api.setTodoTask).toHaveBeenLastCalledWith('slot-1', '2', 'b', true)
    // Slot 2's rows are not held by slot 1's queue.
    const pill = screen.getByTestId('todo-pill')
    if (pill.getAttribute('aria-expanded') !== 'true') await userEvent.click(pill)
    for (const row of screen.getAllByTestId('todo-row-toggle')) expect(row).not.toBeDisabled()
  })

  it('a queued mutation for another slot does not erase this slot\'s failure notice', async () => {
    // Slot 1 fails, slot 2 is queued. When slot 2's tick starts it resets the
    // shared react-query mutation state; the notice must survive because it is
    // retained per slot, not read off tick.error.
    const resolvers: Array<(v: unknown) => void> = []
    let call = 0
    vi.mocked(api.setTodoTask).mockImplementation(() => {
      call += 1
      if (call === 1) return Promise.reject(new Error('403: slot 1 refused'))
      return new Promise(r => { resolvers.push(r) })
    })
    const { store, rerender } = renderBar([
      slot('slot-1', todo([['a', false], ['b', false]])),
      slot('slot-2', todo([['x', false]])),
    ])
    await userEvent.click(screen.getByTestId('todo-pill'))
    const rows = screen.getAllByTestId('todo-row-toggle')
    await userEvent.click(rows[0]) // slot-1 row 1 -> will fail
    await waitFor(() => expect(screen.getByTestId('todo-tick-error')).toBeInTheDocument())
    // The person switches to slot 2 and clicks a row there (queued + mutated).
    rerender(<Provider store={store}><TaskProgressBar slot="slot-2" /></Provider>)
    const pill = screen.getByTestId('todo-pill')
    if (pill.getAttribute('aria-expanded') !== 'true') await userEvent.click(pill)
    await userEvent.click(screen.getAllByTestId('todo-row-toggle')[0]) // slot-2
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(2))
    // Slot 2 shows NO stale notice for slot 1's failure.
    expect(screen.queryByTestId('todo-tick-error')).toBeNull()
    // Switch back to slot 1: its failure notice is still there, un-erased.
    rerender(<Provider store={store}><TaskProgressBar slot="slot-1" /></Provider>)
    const pill1 = screen.getByTestId('todo-pill')
    if (pill1.getAttribute('aria-expanded') !== 'true') await userEvent.click(pill1)
    expect(screen.getByTestId('todo-tick-error')).toBeInTheDocument()
  })

  it('a click after a refusal is sent, not greyed out under the old notice', async () => {
    vi.mocked(api.setTodoTask).mockRejectedValueOnce(new Error('409: stale'))
    renderBar([slot('slot-1', todo([['a', false], ['b', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    const rows = screen.getAllByTestId('todo-row-toggle')
    await userEvent.click(rows[0])
    await waitFor(() => expect(screen.getByTestId('todo-tick-error')).toBeInTheDocument())
    vi.mocked(api.setTodoTask).mockResolvedValue({ ok: true })
    await userEvent.click(rows[1])
    await waitFor(() => expect(api.setTodoTask).toHaveBeenCalledTimes(2))
    expect(api.setTodoTask).toHaveBeenLastCalledWith('slot-1', '2', 'b', true)
  })

  it('a row the person set wears a person glyph and says so, until the agent confirms', async () => {
    const list = todo([['a', false], ['b', true]])
    list.tasks[1] = { ...list.tasks[1], person: true }
    renderBar([slot('slot-1', list)])
    await userEvent.click(screen.getByTestId('todo-pill'))
    expect(screen.getAllByTestId('todo-row-person')).toHaveLength(1)
    const rows = screen.getAllByTestId('todo-row-toggle')
    expect(rows[1].getAttribute('title')).toMatch(/You marked this done; the agent has not confirmed/)
    expect(rows[0].getAttribute('title')).not.toMatch(/You marked/)
  })

  it('a remote-bound session shows the list read-only', async () => {
    const remote = { ...slot('slot-1', todo([['a', false]])), executor: 'remote' as const, instance_id: 'peer-1' }
    renderBar([remote])
    await userEvent.click(screen.getByTestId('todo-pill'))
    expect(screen.getAllByTestId('todo-row')).toHaveLength(1)
    expect(screen.queryByTestId('todo-row-toggle')).toBeNull()
    expect(screen.getByTestId('todo-hint').textContent).toMatch(/read-only/i)
  })

  it('shows a refused PATCH through ErrorNotice instead of a silent unchanged row', async () => {
    vi.mocked(api.setTodoTask).mockRejectedValue(new Error('403: the calling session is gone'))
    renderBar([slot('slot-1', todo([['a', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    await userEvent.click(screen.getByTestId('todo-row-toggle'))
    await waitFor(() => expect(screen.getByTestId('todo-tick-error')).toBeInTheDocument())
    // A human lead, then the server's own words as detail when no plain
    // rendering exists for the failure (here: a bare transport error).
    expect(screen.getByTestId('todo-tick-error').textContent).toMatch(/"a" did not update/)
    expect(screen.getByTestId('todo-tick-error').textContent).toMatch(/calling session is gone/)
    expect(screen.getByTestId('todo-row-toggle')).toHaveAttribute('aria-checked', 'false')
  })

  it.each([
    ['caller_unattributable', 403, /session expired/],
    ['todo_task_stale', 409, /different task now/],
    ['todo_task_not_found', 404, /not in the list any more/],
    ['remote_action_unsupported', 409, /remote machine running this session/],
  ])('renders the gateway refusal %s in plain words, server text on the tooltip', async (code, status, plain) => {
    const serverText = `server said ${code}`
    vi.mocked(api.setTodoTask).mockRejectedValue(
      new ApiError(status, serverText, JSON.stringify({ error: serverText, code })),
    )
    renderBar([slot('slot-1', todo([['a', false]]))])
    await userEvent.click(screen.getByTestId('todo-pill'))
    await userEvent.click(screen.getByTestId('todo-row-toggle'))
    await waitFor(() => expect(screen.getByTestId('todo-tick-error')).toBeInTheDocument())
    const notice = screen.getByTestId('todo-tick-error')
    expect(notice.textContent).toMatch(plain)
    expect(notice.textContent).not.toMatch(serverText)
    expect(notice.querySelector(`[title="${serverText}"]`)).not.toBeNull()
  })
})

describe('sseTodoUpdate reducer', () => {
  const initial = { slots: [slot('slot-1', null), slot('slot-2', null)] } as never

  it('patches the addressed slot only', () => {
    const next = dashboardReducer(initial, sseTodoUpdate({ slot: 'slot-2', todo: todo([['x', false]]) }))
    expect(next.slots[0].todo).toBeNull()
    expect(next.slots[1].todo?.tasks[0].text).toBe('x')
  })

  it('clears a list when the delta carries null', () => {
    const withTodo = { slots: [slot('slot-1', todo([['x', false]]))] } as never
    const next = dashboardReducer(withTodo, sseTodoUpdate({ slot: 'slot-1', todo: null }))
    expect(next.slots[0].todo).toBeNull()
  })

  it('ignores a delta for an unknown slot', () => {
    const next = dashboardReducer(initial, sseTodoUpdate({ slot: 'ghost', todo: todo([['x', false]]) }))
    expect(next.slots).toHaveLength(2)
    expect(next.slots.every(s => s.todo === null)).toBe(true)
  })
})
